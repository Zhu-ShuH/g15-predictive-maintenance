"""Transport execution intervals and terminal outcomes over real HTTP sockets.

Fault injection here checks error accounting; these tests are not capacity data.
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import socketserver
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from g15.query.application import QUERY_SHAPE, HealthQueryApplication
from g15.query.auth import AuthorizationPolicy, TokenVerifier, issue_token
from g15.query.cursor import CursorCodec
from g15.service.http import create_query_server
from tools.query_load.d0_fixture import (
    build_d0_runs,
    build_policy_yaml,
    device_id,
    device_run_id,
)


@pytest.fixture(scope="module", name="stage_runs")
def _stage_runs_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create only the public D0 read fixtures, outside the Obsidian vault."""
    root = tmp_path_factory.mktemp("execution-stage-d0") / "runs"
    build_d0_runs(root)
    return root


@pytest.fixture(name="service")
def _service_fixture(stage_runs: Path):
    """Run one real HTTP server and collect only its transport events."""
    bearer_key = secrets.token_bytes(32)
    application = HealthQueryApplication(
        runs_root=stage_runs,
        token_verifier=TokenVerifier(
            key=bearer_key,
            issuer="issuer-execution-stage",
            audience="audience-execution-stage",
            clock=lambda: 2_000,
        ),
        authorization_policy=AuthorizationPolicy.from_yaml(build_policy_yaml()),
        cursor_codec=CursorCodec(secrets.token_bytes(32)),
        event_sink=lambda _event: None,
    )
    events: list[dict[str, object]] = []
    terminal = threading.Event()

    def collect(event):
        events.append(dict(event))
        if event["stage"] == "response_completed" or event["result"] == "overloaded":
            terminal.set()

    server = create_query_server(
        application,
        event_sink=collect,
        request_timeout_seconds=2.0,
        max_workers=1,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    token = issue_token(
        key=bearer_key,
        issuer="issuer-execution-stage",
        audience="audience-execution-stage",
        subject="subject-d0",
        now=1_900,
    )
    harness = SimpleNamespace(
        application=application,
        runs_root=stage_runs,
        server=server,
        host=host,
        port=port,
        path=f"/v1/runs/{device_run_id(0)}/devices/{device_id(0)}/health",
        token=token,
        events=events,
        terminal=terminal,
        started_ns=time.perf_counter_ns(),
    )
    try:
        yield harness
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _request(service, *, method="GET", path=None, authorized=True, headers=None):
    request_headers = dict(headers or {})
    if authorized:
        request_headers["Authorization"] = f"Bearer {service.token}"
    connection = http.client.HTTPConnection(service.host, service.port, timeout=5)
    try:
        connection.request(method, path or service.path, headers=request_headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _assert_events(service, stages, results, *, forbidden=()):
    assert service.terminal.wait(timeout=5), "transport never emitted a terminal event"
    events = list(service.events)
    assert [event["stage"] for event in events] == stages
    assert [event["result"] for event in events] == results
    request_ids = {event["request_id"] for event in events}
    assert len(request_ids) == 1
    assert all(event["trace_id"] == event["request_id"] for event in events)
    assert all(
        isinstance(event["request_id"], str) and event["request_id"] for event in events
    )
    assert all(event["process_id"] == os.getpid() for event in events)
    times = [event["monotonic_ns"] for event in events]
    assert all(
        isinstance(value, int) and not isinstance(value, bool) for value in times
    )
    assert service.started_ns <= times[0] <= times[-1] <= time.perf_counter_ns()
    assert times == sorted(times)
    required = {
        "timestamp_utc",
        "monotonic_ns",
        "process_id",
        "request_id",
        "trace_id",
        "stage",
        "query_shape",
        "result",
    }
    allowed = required | {
        "actor_ref",
        "permission_domain",
        "manifest_schema_version",
        "execution_started",
        "request_head_complete",
        "delivery_result",
    }
    for event in events:
        assert required <= event.keys() <= allowed
        assert event["query_shape"] == QUERY_SHAPE
        instant = datetime.fromisoformat(event["timestamp_utc"].replace("Z", "+00:00"))
        assert instant.utcoffset() == timezone.utc.utcoffset(instant)
    serialized = json.dumps(events, sort_keys=True)
    for value in (
        service.token,
        "Bearer ",
        "subject-d0",
        service.path,
        device_run_id(0),
        device_id(0),
        str(service.runs_root),
        *forbidden,
    ):
        assert value not in serialized
    return events


@pytest.mark.parametrize(
    "method,path,authorized,status,result,executed",
    [
        ("GET", None, True, 200, "success", True),
        ("GET", None, False, 401, "unauthorized", True),
        ("GET", "/private-target-missing", True, 404, "not_found", True),
        ("GET", "?page_size=private-query-value", True, 400, "invalid_request", True),
        ("POST", None, True, 405, "method_not_allowed", False),
    ],
)
def test_request_terminal_pairs(
    service, method, path, authorized, status, result, executed
):
    """Every parsed request has one admitted terminal path and one request ID."""
    target = service.path + path if path and path.startswith("?") else path
    actual_status, body = _request(
        service,
        method=method,
        path=target,
        authorized=authorized,
    )
    assert actual_status == status
    stages = ["server_received"]
    results = ["received"]
    if executed:
        stages += ["server_executing", "server_execution_completed"]
        results += ["started", result]
    else:
        stages += ["execution_not_started"]
        results += [result]
    if result != "success":
        stages += ["rejected"]
        results += [result]
        assert body["error"] == result
    stages += ["response_completed"]
    results += [result]
    events = _assert_events(
        service,
        stages,
        results,
        forbidden=("private-target-missing", "private-query-value"),
    )
    assert body["request_id"] == events[0]["request_id"]
    assert body["trace_id"] == events[0]["request_id"]


@pytest.mark.parametrize(
    "failure_type,execution_result",
    [(RuntimeError, "internal"), (OSError, "connection_error")],
)
def test_query_failure_has_one_execution_end(
    service, monkeypatch, failure_type, execution_result
):
    """Query failures close their interval before error response delivery."""

    def failed_query(**_kwargs):
        raise failure_type("private-database-path-and-secret")

    monkeypatch.setattr(service.application, "query_authorized", failed_query)
    status, body = _request(service)
    assert status == 500
    assert body["error"] == "internal"
    _assert_events(
        service,
        [
            "server_received",
            "server_executing",
            "server_execution_completed",
            "rejected",
            "response_completed",
        ],
        ["received", "started", execution_result, execution_result, execution_result],
        forbidden=("private-database-path-and-secret",),
    )


def test_socket_write_failure_preserves_successful_execution(service, monkeypatch):
    """A write error cannot erase the already successful query interval."""

    def failed_write(_writer, _payload):
        raise BrokenPipeError("private-write-diagnostic")

    monkeypatch.setattr(socketserver._SocketWriter, "write", failed_write)
    with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError)):
        _request(service)
    _assert_events(
        service,
        [
            "server_received",
            "server_executing",
            "server_execution_completed",
            "rejected",
            "response_completed",
        ],
        ["received", "started", "success", "connection_error", "connection_error"],
        forbidden=("private-write-diagnostic",),
    )


def test_client_reset_after_query_start_has_one_terminal_event(service, monkeypatch):
    """A real peer reset closes delivery once without restarting execution."""
    query_started = threading.Event()
    finish_query = threading.Event()
    original_query = service.application.query_authorized

    def held_query(**kwargs):
        query_started.set()
        assert finish_query.wait(timeout=5)
        return original_query(**kwargs)

    monkeypatch.setattr(service.application, "query_authorized", held_query)
    connection = socket.create_connection((service.host, service.port), timeout=5)
    try:
        connection.sendall(
            (
                f"GET {service.path} HTTP/1.1\r\nHost: local\r\n"
                f"Authorization: Bearer {service.token}\r\n\r\n"
            ).encode("ascii")
        )
        assert query_started.wait(timeout=5)
        linger_format = "hh" if sys.platform == "win32" else "ii"
        connection.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack(linger_format, 1, 0)
        )
        connection.close()
    finally:
        connection.close()
        finish_query.set()
    _assert_events(
        service,
        [
            "server_received",
            "server_executing",
            "server_execution_completed",
            "rejected",
            "response_completed",
        ],
        ["received", "started", "success", "connection_error", "connection_error"],
    )


def test_expect_rejection_never_starts_execution(service):
    """Expect is rejected before the application execution stage begins."""
    connection = socket.create_connection((service.host, service.port), timeout=5)
    try:
        connection.sendall(
            (
                f"GET {service.path} HTTP/1.1\r\nHost: local\r\n"
                f"Authorization: Bearer {service.token}\r\n"
                "Content-Length: 1\r\nExpect: 100-continue\r\n\r\n"
            ).encode("ascii")
        )
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "invalid_request"
    finally:
        connection.close()
    _assert_events(
        service,
        ["server_received", "execution_not_started", "rejected", "response_completed"],
        ["received", "invalid_request", "invalid_request", "invalid_request"],
    )


@pytest.mark.parametrize("complete_head", [True, False])
def test_overload_records_admission_and_delivery_without_execution(
    service, complete_head
):
    """Capacity rejection distinguishes a written 503 from an incomplete head."""
    assert service.server._worker_slots.acquire(blocking=False)
    try:
        if complete_head:
            status, body = _request(service)
            assert status == 503
            assert body["error"] == "internal"
        else:
            with socket.create_connection(
                (service.host, service.port), timeout=5
            ) as connection:
                connection.sendall(b"GET /private-incomplete-target HTTP/1.1\r\n")
                assert connection.recv(1024) == b""
        events = _assert_events(
            service,
            ["rejected"],
            ["overloaded"],
            forbidden=("private-incomplete-target",),
        )
        assert events[0]["execution_started"] is False
        assert events[0]["request_head_complete"] is complete_head
        assert events[0]["delivery_result"] == (
            "written" if complete_head else "request_head_incomplete"
        )
        if complete_head:
            assert body["request_id"] == events[0]["request_id"]
    finally:
        service.server._worker_slots.release()


@pytest.mark.parametrize(
    "framing_headers",
    [
        "Content-Length: 1\r\n",
        "Content-Length: 0\r\nContent-Length: 0\r\n",
        "Transfer-Encoding: chunked\r\n",
    ],
    ids=["nonempty-body", "duplicate-content-length", "transfer-encoding"],
)
def test_non_get_bad_framing_reports_invalid_request_without_execution(
    service, framing_headers
):
    """A malformed non-GET head reports the actual 400 cause at every stage."""
    with socket.create_connection(
        (service.host, service.port), timeout=5
    ) as connection:
        connection.sendall(
            (
                f"POST {service.path} HTTP/1.1\r\nHost: local\r\n"
                f"Authorization: Bearer {service.token}\r\n"
                f"{framing_headers}\r\n"
            ).encode("ascii")
        )
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 400
        body = json.loads(response.read())
        assert body["error"] == "invalid_request"
    events = _assert_events(
        service,
        ["server_received", "execution_not_started", "rejected", "response_completed"],
        ["received", "invalid_request", "invalid_request", "invalid_request"],
    )
    assert body["request_id"] == events[0]["request_id"]
    assert body["trace_id"] == events[0]["request_id"]
