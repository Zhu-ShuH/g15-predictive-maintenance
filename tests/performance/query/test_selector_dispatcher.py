"""Real TCP and deterministic scheduling regressions, never capacity claims."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
from urllib.parse import urlsplit

import pytest

from tools.query_load import dispatcher
from tools.query_load.accounting import RunAccounting
from tools.query_load.dispatcher import RequestSpec, dispatch_wave
from tools.query_load.network import SocketNetworkMeter, validate_network_pair
from tools.query_load.preparation import ReaderReadiness

_TOKEN = "private-selector-bearer"


def _reference(sock):
    local, peer = sock.getsockname(), sock.getpeername()
    return {
        "local": {"host": str(local[0]), "port": int(local[1])},
        "peer": {"host": str(peer[0]), "port": int(peer[1])},
    }


def _wire(body, status=200, headers=None):
    fields = [("Content-Length", str(len(body))), ("Connection", "close")]
    if headers is not None:
        fields = headers
    return (
        f"HTTP/1.1 {status} Test\r\n".encode()
        + b"".join(f"{key}: {value}\r\n".encode() for key, value in fields)
        + b"\r\n"
        + body
    )


def _success(handler):
    device = urlsplit(handler.path).path.split("/")[-2]
    body = json.dumps({"request_id": device, "trace_id": device, "ok": True}).encode()
    handler.connection.sendall(_wire(body))


@contextmanager
def _endpoint(respond=_success, expected=1):
    meter = SocketNetworkMeter("server")
    readiness = ReaderReadiness()
    requests, errors = [], []
    drained = threading.Event()
    closed = 0
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            readiness.installed(_reference(self.connection))

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            try:
                respond(self)
            except Exception as exc:
                errors.append(type(exc).__name__)

        def log_message(self, *_args):
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def get_request(self):
            connection, address = super().get_request()
            return meter.wrap(connection), address

        def process_request_thread(self, request, client_address):
            nonlocal closed
            reference = _reference(request)
            try:
                super().process_request_thread(request, client_address)
            finally:
                readiness.closed(reference)
                with lock:
                    closed += 1
                    if closed == expected:
                        drained.set()

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.005), daemon=True
    )
    thread.start()
    host, port = server.server_address
    value = {
        "url": f"http://{host}:{port}",
        "meter": meter,
        "requests": requests,
        "errors": errors,
        "drained": drained,
        "readiness": readiness,
    }
    try:
        yield value
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def _spec(url, suffix="a", **kwargs):
    return RequestSpec(
        url, f"actor-{suffix}", _TOKEN, f"run-{suffix}", f"device-{suffix}", **kwargs
    )


def _run(endpoint, specs=None, *, timeout=1, emit=None):
    accounting, meter, retained = RunAccounting(), SocketNetworkMeter("client"), []

    def confirm(references):
        assert endpoint["requests"] == []
        endpoint["readiness"].wait(references, timeout=2)

    def capture(record, mapping):
        retained.append((record, mapping))
        if emit is not None:
            emit(record, mapping)

    results = dispatch_wave(
        specs or [_spec(endpoint["url"])],
        accounting=accounting,
        meter=meter,
        confirm_preparation=confirm,
        emit_completed=capture,
        timeout_seconds=timeout,
    )
    return results, accounting, meter, retained


def test_request_spec_is_frozen_and_bearer_is_not_in_repr():
    spec = _spec("http://127.0.0.1:1234")
    assert _TOKEN not in repr(spec)
    with pytest.raises(FrozenInstanceError):
        spec.actor = "changed"


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "http://127.0.0.1:1234/path"},
        {"base_url": "http://user:secret@127.0.0.1:1234"},
        {"base_url": "https://127.0.0.1:1234"},
        {"base_url": "http://192.0.2.1:1234"},
        {"base_url": "http://127.0.0.1:1234?destination=other"},
        {"token": "secret\r\nInjected: header"},
        {"page_size": True},
        {"page_size": 101},
        {"streams": "unknown"},
    ],
)
def test_invalid_direct_request_configuration_is_rejected(change):
    fields = dict(
        base_url="http://127.0.0.1:1234",
        actor="a",
        token=_TOKEN,
        run_id="r",
        device_id="d",
    )
    fields.update(change)
    with pytest.raises(ValueError):
        RequestSpec(**fields)


def test_actual_requests_keep_independent_timing_payload_and_network_pair(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    with _endpoint(expected=2) as endpoint:
        results, accounting, meter, retained = _run(
            endpoint,
            [_spec(endpoint["url"], "a", streams="both"), _spec(endpoint["url"], "b")],
        )
        assert endpoint["drained"].wait(2)
        assert not endpoint["errors"]
        assert len(results) == len(accounting.client_records) == len(retained) == 2
        assert (
            validate_network_pair(meter.snapshot(), endpoint["meter"].snapshot()) == []
        )
        assert all(auth == f"Bearer {_TOKEN}" for _path, auth in endpoint["requests"])
        assert any("streams=both" in path for path, _auth in endpoint["requests"])
        assert not meter.active_count
        for record, mapping in retained:
            assert record.error == ""
            assert record.status == 200
            assert (
                json.loads(base64.b64decode(record.response_bytes_base64))["request_id"]
                == record.server_request_id
            )
            times = mapping["preparation"]
            assert (
                times["connect_started_ns"]
                <= times["connected_ns"]
                <= times["headers_prepared_ns"]
                <= times["launch_released_ns"]
                <= times["send_started_ns"]
                <= times["send_completed_ns"]
                <= times["body_complete_ns"]
                <= times["decoded_ns"]
            )
            assert times["http_started"] is times["request_sent"] is True
            assert times["launch_mode"] == "selector"
            assert times["selector_backend"].endswith("Selector")
            assert "barrier_released_ns" not in times
            assert (
                record.started_monotonic
                <= record.waiting_monotonic
                <= record.completed_monotonic
            )
            assert mapping["timeout_seconds"] == 1
        assert (
            len(
                {
                    mapping["preparation"]["launch_released_ns"]
                    for _record, mapping in retained
                }
            )
            == 1
        )
        public = json.dumps([[asdict(record), mapping] for record, mapping in retained])
        assert _TOKEN not in public
        assert "Authorization" not in public


def test_complete_content_length_finishes_without_waiting_for_eof():
    release = threading.Event()

    def reply_keep_open(handler):
        _success(handler)
        assert release.wait(2)

    with _endpoint(reply_keep_open) as endpoint:
        try:
            results, _accounting, meter, retained = _run(endpoint, timeout=0.5)
            assert results[0][0] == 200
            assert retained[0][0].error == ""
            assert retained[0][1]["preparation"]["body_complete_ns"] is not None
            assert not release.is_set()
            assert not meter.active_count
        finally:
            release.set()
        assert endpoint["drained"].wait(2)
        assert not endpoint["errors"]


def test_ready_response_completes_before_another_request_finishes_partial_send(
    monkeypatch,
):
    """A real response is read/decoded while B is still short-writing/EAGAIN."""
    blocked, a_complete = threading.Event(), threading.Event()
    history = []

    def respond(handler):
        if "/device-a/" in handler.path:
            assert blocked.wait(2)
        _success(handler)

    original = dispatcher.MeteredHTTPConnection
    created = 0

    class ScheduledSocket:
        def __init__(self, sock, label):
            self.sock, self.label, self.first = sock, label, True

        def __getattr__(self, name):
            return getattr(self.sock, name)

        def send(self, data):
            if self.label == "b":
                if self.first:
                    self.first = False
                    count = self.sock.send(data[:1])
                    history.append("b_partial")
                    return count
                if not a_complete.is_set():
                    history.append("b_would_block")
                    blocked.set()
                    raise BlockingIOError(errno.EAGAIN, "test scheduling; no progress")
                count = self.sock.send(data)
                if count == len(data):
                    history.append("b_send_complete")
                return count
            return self.sock.send(data)

        def recv(self, size):
            data = self.sock.recv(size)
            if data and self.label == "a":
                history.append("a_recv")
            return data

    class ScheduledConnection(original):
        def connect(self):
            nonlocal created
            super().connect()
            label = "a" if created == 0 else "b"
            created += 1
            self.sock = ScheduledSocket(self.sock, label)

    monkeypatch.setattr(dispatcher, "MeteredHTTPConnection", ScheduledConnection)

    def completed(record, _mapping):
        if record.server_request_id == "device-a":
            assert json.loads(base64.b64decode(record.response_bytes_base64))["ok"]
            history.append("a_decoded_complete")
            a_complete.set()

    with _endpoint(respond, expected=2) as endpoint:
        results, accounting, meter, retained = _run(
            endpoint,
            [_spec(endpoint["url"], "a"), _spec(endpoint["url"], "b")],
            emit=completed,
            timeout=1,
        )
        assert all(status == 200 for status, _body, _run_id in results)
        assert all(not record.error for record, _mapping in retained)
        assert len(accounting.client_records) == 2
        assert "b_would_block" in history
        assert (
            history.index("a_recv")
            < history.index("a_decoded_complete")
            < history.index("b_send_complete")
        )
        assert endpoint["drained"].wait(2)
        assert (
            validate_network_pair(meter.snapshot(), endpoint["meter"].snapshot()) == []
        )
        assert not endpoint["errors"]


@pytest.mark.parametrize(
    ("body", "status", "headers", "expected_status", "error"),
    [
        (b'{"request_id":"error-id"}', 401, None, 401, "http_status_401"),
        (b'{"request_id":"error-id"}', 503, None, 503, "http_status_503"),
        (
            b"{}",
            302,
            [("Content-Length", "2"), ("Location", "http://127.0.0.1:1")],
            302,
            "http_status_302",
        ),
        (b"not-json", 200, None, 200, "invalid_json"),
        (b"[]", 200, None, 200, "non_object_json"),
        (b'{"score":NaN}', 200, None, 200, "invalid_json"),
        (b'{"score":1e999}', 200, None, 200, "invalid_json"),
        (b"{}", 200, [], 200, "ResponseFramingError"),
        (
            b"{}",
            200,
            [("Content-Length", "2"), ("Content-Length", "2")],
            200,
            "ResponseFramingError",
        ),
        (
            b"{}",
            200,
            [("Content-Length", "2"), ("Transfer-Encoding", "chunked")],
            200,
            "ResponseFramingError",
        ),
        (b"{}", 200, [("Content-Length", "-2")], 200, "ResponseFramingError"),
        (b'{"part":1}', 200, [("Content-Length", "100")], 200, "EOFError"),
        (b"{}excess", 200, [("Content-Length", "2")], 200, "ResponseFramingError"),
    ],
)
def test_http_decode_and_framing_errors_preserve_actual_body(
    body, status, headers, expected_status, error
):
    def respond(handler):
        handler.connection.sendall(_wire(body, status, headers))

    with _endpoint(respond) as endpoint:
        results, accounting, meter, retained = _run(endpoint)
        record, mapping = retained[0]
        assert record.status == results[0][0] == expected_status
        assert record.error == error
        assert base64.b64decode(record.response_bytes_base64) == body
        assert len(accounting.client_records) == 1
        assert mapping["preparation"]["http_started"]
        assert not meter.active_count
        assert endpoint["drained"].wait(2)
        assert not endpoint["errors"]


def test_incremental_status_headers_and_body_are_read_through_real_socket(monkeypatch):
    monkeypatch.setattr(dispatcher, "_READ_BYTES", 3)
    with _endpoint() as endpoint:
        results, _accounting, meter, retained = _run(endpoint)
        assert results[0][0] == 200
        assert not retained[0][0].error
        events = meter.snapshot()["connections"][0]["io_events"]
        positive_reads = [
            event
            for event in events
            if event["operation"] == "recv" and event["returned_bytes"]
        ]
        assert len(positive_reads) > 20
        assert all(event["returned_bytes"] <= 3 for event in positive_reads)


@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt])
def test_failed_ack_never_sends_or_invents_http_in_flight_and_preserves_original(
    failure,
):
    with _endpoint(expected=2) as endpoint:
        accounting, meter, retained = RunAccounting(), SocketNetworkMeter("client"), []
        original = failure("private control detail")

        def confirm(references):
            endpoint["readiness"].wait(references, timeout=2)
            assert endpoint["requests"] == []
            raise original

        with pytest.raises(failure) as raised:
            dispatch_wave(
                [_spec(endpoint["url"], "a"), _spec(endpoint["url"], "b")],
                accounting=accounting,
                meter=meter,
                confirm_preparation=confirm,
                emit_completed=lambda record, mapping: retained.append(
                    (record, mapping)
                ),
            )
        assert raised.value is original
        assert len(retained) == 2
        assert accounting.client_records == []
        assert accounting._client_started == 0
        assert accounting._client_in_flight == 0
        assert meter.active_count == 0
        assert endpoint["drained"].wait(2)
        assert endpoint["requests"] == []
        for record, mapping in retained:
            assert record.status == 0
            assert (
                record.started_monotonic
                == record.waiting_monotonic
                == record.completed_monotonic
            )
            assert not mapping["latency_eligible"]
            assert not mapping["preparation"]["http_started"]
            assert mapping["preparation"]["send_started_ns"] is None
        assert "private control detail" not in json.dumps(
            [[asdict(record), mapping] for record, mapping in retained]
        )


def test_failed_connect_retains_all_unsent_attempts_without_memory_starts():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    host, port = listener.getsockname()
    listener.close()
    accounting, meter, retained = RunAccounting(), SocketNetworkMeter("client"), []
    with pytest.raises(OSError):
        dispatch_wave(
            [_spec(f"http://{host}:{port}", "a"), _spec(f"http://{host}:{port}", "b")],
            accounting=accounting,
            meter=meter,
            confirm_preparation=lambda _references: pytest.fail(
                "must not confirm failed TCP"
            ),
            emit_completed=lambda record, mapping: retained.append((record, mapping)),
            timeout_seconds=0.1,
        )
    assert len(retained) == 2
    assert accounting.client_records == []
    assert not meter.active_count
    assert len(meter.snapshot()["connections"]) == 1
    assert all(
        not mapping["preparation"]["http_started"] for _record, mapping in retained
    )


def test_read_inactivity_timeout_retains_a_real_started_request():
    release = threading.Event()

    def never_reply(_handler):
        assert release.wait(2)

    with _endpoint(never_reply) as endpoint:
        try:
            results, accounting, meter, retained = _run(endpoint, timeout=0.03)
            assert results[0][0] == 0
            assert retained[0][0].error == "TimeoutError"
            assert retained[0][1]["preparation"]["request_sent"]
            assert len(accounting.client_records) == 1
            assert not accounting._client_in_flight
            assert not meter.active_count
        finally:
            release.set()
        assert endpoint["drained"].wait(2)


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_partial_send_interrupt_keeps_attempts_and_propagates_original(
    monkeypatch, failure
):
    original_connection = dispatcher.MeteredHTTPConnection
    original = failure("private interruption detail")

    class InterruptRawSocket:
        def __init__(self, sock):
            self.sock = sock

        def __getattr__(self, name):
            return getattr(self.sock, name)

        def send(self, data, *_flags):
            self.sock.send(data[:3])
            raise original

    class InterruptConnection(original_connection):
        def connect(self):
            super().connect()
            self.sock._sock = InterruptRawSocket(self.sock._sock)

    monkeypatch.setattr(dispatcher, "MeteredHTTPConnection", InterruptConnection)
    with _endpoint(expected=2) as endpoint:
        accounting, meter, retained = RunAccounting(), SocketNetworkMeter("client"), []
        with pytest.raises(failure) as raised:
            dispatch_wave(
                [_spec(endpoint["url"], "a"), _spec(endpoint["url"], "b")],
                accounting=accounting,
                meter=meter,
                confirm_preparation=lambda refs: endpoint["readiness"].wait(
                    refs, timeout=2
                ),
                emit_completed=lambda record, mapping: retained.append(
                    (record, mapping)
                ),
            )
        assert raised.value is original
        assert len(retained) == 2
        assert len(accounting.client_records) == 1
        assert (
            sum(mapping["preparation"]["http_started"] for _record, mapping in retained)
            == 1
        )
        assert not accounting._client_in_flight
        assert not meter.active_count
        assert meter.snapshot()["send_partial_unknown"]
        assert endpoint["drained"].wait(2)
        assert "private interruption detail" not in json.dumps(
            [[asdict(record), mapping] for record, mapping in retained]
        )


def test_journal_failure_is_not_swallowed_and_each_emit_is_attempted_once():
    with _endpoint(expected=2) as endpoint:
        accounting, meter, seen = RunAccounting(), SocketNetworkMeter("client"), []
        original = RuntimeError("private journal error")

        def broken_journal(record, mapping):
            seen.append((record.user_id, deepcopy(mapping)))
            raise original

        with pytest.raises(RuntimeError) as raised:
            dispatch_wave(
                [_spec(endpoint["url"], "a"), _spec(endpoint["url"], "b")],
                accounting=accounting,
                meter=meter,
                confirm_preparation=lambda refs: endpoint["readiness"].wait(
                    refs, timeout=2
                ),
                emit_completed=broken_journal,
            )
        assert raised.value is original
        assert sorted(actor for actor, _mapping in seen) == ["actor-a", "actor-b"]
        assert not accounting._client_in_flight
        assert not meter.active_count
        assert endpoint["drained"].wait(2)


def test_real_health_endpoint_keeps_v1_v2_oracle_and_readonly_sqlite(tmp_path):
    import hashlib

    from g15.query.auth import issue_token
    from tools.query_load.d0_fixture import (
        D0_ACTOR_REF,
        D0_SUBJECT,
        build_d0_runs,
        device_id,
        device_run_id,
    )
    from tools.query_load.driver import (
        BEARER_AUDIENCE,
        BEARER_ISSUER,
        build_application,
        start_server,
    )
    from tools.query_load.oracle import verify_response

    root = tmp_path / "real-query"
    paths = build_d0_runs(root)
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    bearer, cursor = b"b" * 32, b"c" * 32
    app, _events = build_application(root, bearer, cursor)
    accounting, meter, retained = RunAccounting(), SocketNetworkMeter("client"), []
    server, thread, url = start_server(app, accounting)
    token = issue_token(
        key=bearer,
        issuer=BEARER_ISSUER,
        audience=BEARER_AUDIENCE,
        subject=D0_SUBJECT,
        now=1900,
    )
    try:
        for streams in (None, "both", "windows", "alarms"):
            results = dispatch_wave(
                [
                    RequestSpec(
                        url,
                        D0_ACTOR_REF,
                        token,
                        device_run_id(0),
                        device_id(0),
                        page_size=10,
                        streams=streams,
                    )
                ],
                accounting=accounting,
                meter=meter,
                confirm_preparation=lambda references: None,
                emit_completed=lambda record, mapping: retained.append(
                    (record, mapping)
                ),
            )
            status, body, run_id = results[0]
            assert status == 200
            assert (
                verify_response(
                    body,
                    paths[0],
                    run_id,
                    device_id(0),
                    page_size=10,
                    first_page=True,
                    streams=streams,
                )
                == []
            )
            assert body["query_contract_version"] == ("1" if streams is None else "2")
            if streams is not None:
                assert body["windows"]["omitted"] is (streams == "alarms")
                assert body["alarms"]["omitted"] is (streams == "windows")
        assert len(retained) == len(accounting.client_records) == 4
        assert all(not record.error for record, _mapping in retained)
        assert before == [
            hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
        ]
        assert not meter.active_count
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_wave_body_budget_failure_keeps_known_bodies_and_closes_every_socket(
    monkeypatch,
):
    body = json.dumps({"padding": "x" * 45}).encode()
    assert len(body) <= 100 < len(body) * 2
    monkeypatch.setattr(dispatcher, "MAX_BODY_BYTES", 100)
    with _endpoint(
        lambda handler: handler.connection.sendall(_wire(body)), expected=2
    ) as endpoint:
        accounting, meter, retained = RunAccounting(), SocketNetworkMeter("client"), []
        with pytest.raises(dispatcher.ResponseBudgetError):
            dispatch_wave(
                [_spec(endpoint["url"], "a"), _spec(endpoint["url"], "b")],
                accounting=accounting,
                meter=meter,
                confirm_preparation=lambda refs: endpoint["readiness"].wait(
                    refs, timeout=2
                ),
                emit_completed=lambda record, mapping: retained.append(
                    (record, mapping)
                ),
            )
        assert len(retained) == len(accounting.client_records) == 2
        assert (
            sum(
                len(base64.b64decode(record.response_bytes_base64))
                for record, _mapping in retained
            )
            == len(body) * 2
        )
        assert any(
            record.error == "ResponseBudgetError" for record, _mapping in retained
        )
        assert not meter.active_count
        assert not accounting._client_in_flight
        assert endpoint["drained"].wait(2)


class _VirtualClock:
    """Advance unit-test client time on real I/O, with no sleeps or wire holds."""

    def __init__(self, real):
        self.real, self.offset = real, 0.0

    def perf_counter(self):
        return self.real.perf_counter() + self.offset

    def perf_counter_ns(self):
        return self.real.perf_counter_ns() + int(self.offset * 1_000_000_000)


@pytest.mark.parametrize("phase", ["send", "recv"])
def test_would_block_does_not_reset_phase_timeout(monkeypatch, phase):
    clock = _VirtualClock(dispatcher.time)
    monkeypatch.setattr(dispatcher, "time", clock)
    original = dispatcher.MeteredHTTPConnection

    class NoProgressSocket:
        def __init__(self, sock):
            self.sock = sock

        def __getattr__(self, name):
            return getattr(self.sock, name)

        def send(self, data, *flags):
            if phase == "send":
                clock.offset += 0.02
                raise BlockingIOError(errno.EAGAIN, "no progress")
            return self.sock.send(data, *flags)

        def recv(self, size, *flags):
            clock.offset += 0.02
            raise BlockingIOError(errno.EAGAIN, "no progress")

    class NoProgressConnection(original):
        def connect(self):
            super().connect()
            self.sock._sock = NoProgressSocket(self.sock._sock)

    monkeypatch.setattr(dispatcher, "MeteredHTTPConnection", NoProgressConnection)
    with _endpoint() as endpoint:
        _results, accounting, meter, retained = _run(endpoint, timeout=0.05)
        assert retained[0][0].error == "TimeoutError"
        assert retained[0][1]["preparation"]["request_sent"] is (phase == "recv")
        assert len(accounting.client_records) == 1
        assert not accounting._client_in_flight
        snapshot = meter.snapshot()
        assert not snapshot["errors"]
        assert not snapshot["send_partial_unknown"]
        assert not snapshot["receive_partial_unknown"]
        assert any(
            event.get("would_block")
            for event in snapshot["connections"][0]["io_events"]
        )
        assert not meter.active_count
        assert endpoint["drained"].wait(2)


def test_positive_receive_progress_resets_inactivity_and_json_has_no_cpu_deadline(
    monkeypatch,
):
    clock = _VirtualClock(dispatcher.time)
    monkeypatch.setattr(dispatcher, "time", clock)
    monkeypatch.setattr(dispatcher, "_READ_BYTES", 3)
    original_connection, original_json = (
        dispatcher.MeteredHTTPConnection,
        dispatcher.json,
    )

    class ProgressSocket:
        def __init__(self, sock):
            self.sock = sock

        def __getattr__(self, name):
            return getattr(self.sock, name)

        def recv(self, size, *flags):
            data = self.sock.recv(size, *flags)
            if data:
                clock.offset += 0.04
            return data

    class ProgressConnection(original_connection):
        def connect(self):
            super().connect()
            self.sock._sock = ProgressSocket(self.sock._sock)

    class ExpensiveDecode:
        @staticmethod
        def loads(*args, **kwargs):
            result = original_json.loads(*args, **kwargs)
            clock.offset += 10
            return result

    monkeypatch.setattr(dispatcher, "MeteredHTTPConnection", ProgressConnection)
    monkeypatch.setattr(dispatcher, "json", ExpensiveDecode)
    with _endpoint() as endpoint:
        results, _accounting, meter, retained = _run(endpoint, timeout=0.05)
        record, mapping = retained[0]
        assert results[0][0] == 200
        assert record.error == ""
        assert clock.offset > 10.1
        assert (
            mapping["preparation"]["decoded_ns"]
            > mapping["preparation"]["body_complete_ns"]
        )
        assert not meter.active_count
        assert endpoint["drained"].wait(2)


@pytest.mark.parametrize("phase", ["send", "recv"])
def test_unexpected_blocking_error_is_a_failure_not_normal_readiness(
    monkeypatch, phase
):
    original = dispatcher.MeteredHTTPConnection

    class BrokenSocket:
        def __init__(self, sock):
            self.sock = sock

        def __getattr__(self, name):
            return getattr(self.sock, name)

        def send(self, data, *flags):
            if phase == "send":
                raise BlockingIOError(errno.EPERM, "private not-would-block error")
            return self.sock.send(data, *flags)

        def recv(self, _size, *_flags):
            raise BlockingIOError(errno.EPERM, "private not-would-block error")

    class BrokenConnection(original):
        def connect(self):
            super().connect()
            self.sock._sock = BrokenSocket(self.sock._sock)

    monkeypatch.setattr(dispatcher, "MeteredHTTPConnection", BrokenConnection)
    with _endpoint() as endpoint:
        _results, accounting, meter, retained = _run(endpoint)
        assert retained[0][0].error == "BlockingIOError"
        assert len(accounting.client_records) == 1
        assert meter.snapshot()["errors"]
        assert not meter.active_count
        assert endpoint["drained"].wait(2)
        assert "private not-would-block error" not in json.dumps(
            [[asdict(record), mapping] for record, mapping in retained]
        )


def test_fresh_read_ready_is_checked_between_writes_from_one_ready_batch(monkeypatch):
    """A response arriving during B's short write must precede C's write."""
    b_partial, a_response_sent, a_completed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    history = []
    original_connection, original_selector = (
        dispatcher.MeteredHTTPConnection,
        dispatcher.selectors.DefaultSelector,
    )
    created = 0

    def respond(handler):
        if "/device-a/" in handler.path:
            assert b_partial.wait(2)
            _success(handler)
            a_response_sent.set()
        else:
            _success(handler)

    class OrderedSelector:
        def __init__(self):
            self.selector = original_selector()

        def __getattr__(self, name):
            return getattr(self.selector, name)

        def select(self, timeout=None):
            return sorted(
                self.selector.select(timeout),
                key=lambda item: item[0].data.spec.device_id,
            )

    class CoordinatedSocket:
        def __init__(self, sock, label):
            self.sock, self.label, self.first = sock, label, True

        def __getattr__(self, name):
            return getattr(self.sock, name)

        def send(self, data):
            if self.label == "b" and self.first:
                self.first = False
                count = self.sock.send(data[:1])
                b_partial.set()
                assert a_response_sent.wait(2)
                history.append("a_response_available_after_b_short_write")
                return count
            if self.label == "c":
                history.append("c_write")
                assert a_completed.is_set()
            return self.sock.send(data)

        def recv(self, size):
            result = self.sock.recv(size)
            if result and self.label == "a":
                history.append("a_read")
            return result

    class CoordinatedConnection(original_connection):
        def connect(self):
            nonlocal created
            super().connect()
            self.sock = CoordinatedSocket(self.sock, "abc"[created])
            created += 1

    monkeypatch.setattr(dispatcher, "MeteredHTTPConnection", CoordinatedConnection)
    monkeypatch.setattr(dispatcher.selectors, "DefaultSelector", OrderedSelector)

    def completed(record, _mapping):
        if record.server_request_id == "device-a":
            history.append("a_decoded_completed")
            a_completed.set()

    with _endpoint(respond, expected=3) as endpoint:
        results, _accounting, meter, retained = _run(
            endpoint, [_spec(endpoint["url"], label) for label in "abc"], emit=completed
        )
        assert all(status == 200 for status, _body, _run_id in results)
        assert all(not record.error for record, _mapping in retained)
        assert (
            history.index("a_response_available_after_b_short_write")
            < history.index("a_read")
            < history.index("a_decoded_completed")
            < history.index("c_write")
        )
        assert endpoint["drained"].wait(2)
        assert (
            validate_network_pair(meter.snapshot(), endpoint["meter"].snapshot()) == []
        )
        assert not endpoint["errors"]


def test_close_failure_keeps_original_and_owned_socket_cleanup_after_http_owner_clears_it(
    monkeypatch,
):
    original_connection = dispatcher.MeteredHTTPConnection
    original = OSError("private failed close")
    failed = False

    class CloseOnceFails:
        def __init__(self, sock):
            self.sock = sock

        def __getattr__(self, name):
            return getattr(self.sock, name)

        def close(self):
            nonlocal failed
            if not failed:
                failed = True
                raise original
            self.sock.close()

    class CloseConnection(original_connection):
        def connect(self):
            super().connect()
            self.sock._sock = CloseOnceFails(self.sock._sock)

    monkeypatch.setattr(dispatcher, "MeteredHTTPConnection", CloseConnection)
    with _endpoint(expected=2) as endpoint:
        accounting, meter, retained = RunAccounting(), SocketNetworkMeter("client"), []
        with pytest.raises(OSError) as raised:
            dispatch_wave(
                [_spec(endpoint["url"], "a"), _spec(endpoint["url"], "b")],
                accounting=accounting,
                meter=meter,
                confirm_preparation=lambda refs: endpoint["readiness"].wait(
                    refs, timeout=2
                ),
                emit_completed=lambda record, mapping: retained.append(
                    (record, mapping)
                ),
            )
        assert raised.value is original
        assert len(retained) == 2
        assert len({record.user_id for record, _mapping in retained}) == 2
        assert not accounting._client_in_flight
        assert not meter.active_count
        assert any(row["operation"] == "close" for row in meter.snapshot()["errors"])
        assert endpoint["drained"].wait(2)
        assert "private failed close" not in json.dumps(
            [[asdict(record), mapping] for record, mapping in retained]
        )


@pytest.mark.parametrize("late_phase", ["before_remaining_write", "during_final_write"])
def test_expired_total_send_deadline_cannot_be_replaced_by_a_fresh_read_timeout(
    monkeypatch, late_phase
):
    clock = _VirtualClock(dispatcher.time)
    monkeypatch.setattr(dispatcher, "time", clock)
    original_connection, original_selector = (
        dispatcher.MeteredHTTPConnection,
        dispatcher.selectors.DefaultSelector,
    )
    partial = False
    advanced = False

    class DelayedReadySelector:
        def __init__(self):
            self.selector = original_selector()

        def __getattr__(self, name):
            return getattr(self.selector, name)

        def select(self, timeout=None):
            nonlocal advanced
            ready = self.selector.select(timeout)
            if partial and timeout and not advanced:
                # Model select/OS scheduling crossing the send deadline after
                # one genuine partial send, before the next ready callback.
                advanced = True
                clock.offset += 1
            return ready

    class TimedRawSocket:
        def __init__(self, sock):
            self.sock = sock

        def __getattr__(self, name):
            return getattr(self.sock, name)

        def send(self, data, *flags):
            nonlocal partial
            if late_phase == "before_remaining_write" and not partial:
                count = self.sock.send(data[:1], *flags)
                partial = True
                return count
            count = self.sock.send(data, *flags)
            if late_phase == "during_final_write":
                clock.offset += 1
            return count

    class TimedConnection(original_connection):
        def connect(self):
            super().connect()
            self.sock._sock = TimedRawSocket(self.sock._sock)

    monkeypatch.setattr(dispatcher, "MeteredHTTPConnection", TimedConnection)
    monkeypatch.setattr(dispatcher.selectors, "DefaultSelector", DelayedReadySelector)
    with _endpoint() as endpoint:
        _results, accounting, meter, retained = _run(endpoint, timeout=0.05)
        record, mapping = retained[0]
        assert record.error == "TimeoutError"
        assert len(accounting.client_records) == 1
        assert not accounting._client_in_flight
        assert not meter.active_count
        assert mapping["preparation"]["request_sent"] is (
            late_phase == "during_final_write"
        )
        assert (mapping["preparation"]["send_completed_ns"] is not None) is (
            late_phase == "during_final_write"
        )
        if late_phase == "before_remaining_write":
            assert meter.snapshot()["bytes_sent_known"] == 1
        else:
            assert meter.snapshot()["bytes_sent_known"] > 1
        assert not meter.snapshot()["send_partial_unknown"]
        assert endpoint["drained"].wait(2)
