"""Real-loopback stream accounting; these are correctness tests, not capacity."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import threading
from urllib import request as urllib_request
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from tools.query_load import client as client_module
from tools.query_load.accounting import RunAccounting
from tools.query_load.network import (
    MeteredHTTPConnection,
    MeteredHTTPHandler,
    SocketNetworkMeter,
    validate_network_pair,
)


@contextmanager
def _tcp_pair():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    left = socket.create_connection(listener.getsockname(), timeout=2)
    right, _peer = listener.accept()
    listener.close()
    left.settimeout(2)
    right.settimeout(2)
    try:
        yield left, right
    finally:
        left.close()
        right.close()


@pytest.fixture(params=["environment", "system_discovery", "installed_opener"])
def hostile_proxy(monkeypatch, request):
    """Use an actual proxy trap without changing user environment or registry."""
    calls, discovery = [], []

    class ProxyTrap(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.command)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(502)
            self.send_header("Content-Length", "2")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"{}")
            self.close_connection = True

        do_GET = do_POST

        def log_message(self, *_args):
            pass

    proxy = HTTPServer(("127.0.0.1", 0), ProxyTrap)
    thread = threading.Thread(
        target=lambda: proxy.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    host, port = proxy.server_address
    proxy_url = f"http://{host}:{port}"
    for variable in (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ):
        monkeypatch.setenv(variable, proxy_url)
    for variable in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(variable, "")
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    original_discovery = urllib_request.getproxies

    def discover():
        discovery.append(request.param)
        if request.param == "system_discovery":
            # Model a registry/system result without reading or changing it.
            return {"http": proxy_url, "https": proxy_url}
        return original_discovery()

    monkeypatch.setattr(urllib_request, "getproxies", discover)
    if request.param == "installed_opener":
        installed = build_opener(
            urllib_request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
    else:
        installed = None
    monkeypatch.setattr(urllib_request, "_opener", installed)
    try:
        yield calls, discovery
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "entrypoint", ["http_connection", "urllib_opener", "query_client"]
)
def test_real_http_counts_headers_body_and_peer_endpoints_without_payload(
    entrypoint, hostile_proxy, monkeypatch
):
    request_body = b"" if entrypoint == "query_client" else b"private-request-body"
    response_body = (
        b'{"request_id":"network-id","trace_id":"network-id"}'
        if entrypoint == "query_client"
        else b"private-response-body"
    )
    target_path = (
        "/v1/runs/public-run/devices/public-device/health"
        if entrypoint == "query_client"
        else "/private-path"
    )
    token = "private-bearer-token"
    client_meter, server_meter = SocketNetworkMeter("client"), SocketNetworkMeter(
        "server"
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            assert self.path == target_path
            assert self.headers["Authorization"] == f"Bearer {token}"
            assert (
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                == request_body
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response_body)
            self.close_connection = True

        do_GET = do_POST

        def log_message(self, *_args):
            pass

    class Server(HTTPServer):
        def get_request(self):
            sock, address = super().get_request()
            return server_meter.wrap(sock), address

    server = Server(("127.0.0.1", 0), Handler)
    server.timeout = 0.5
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    host, port = server.server_address
    try:
        if entrypoint == "http_connection":
            connection = MeteredHTTPConnection(
                host, port, timeout=2, network_meter=client_meter
            )
            try:
                connection.request(
                    "POST",
                    "/private-path",
                    body=request_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response = connection.getresponse()
                assert response.status == 200
                assert response.read() == response_body
                response.close()
            finally:
                connection.close()
        elif entrypoint == "query_client":

            def metered_opener(*handlers):
                return build_opener(
                    *handlers, MeteredHTTPHandler(network_meter=client_meter)
                )

            monkeypatch.setattr(
                client_module, "build_opener", metered_opener, raising=False
            )
            accounting = RunAccounting()
            client = client_module.QueryClient(
                f"http://{host}:{port}", token, "network-user", accounting
            )
            assert client.query("public-run", "public-device") == (
                200,
                json.loads(response_body),
            )
            assert len(accounting.client_records) == 1
            assert accounting.client_records[0].status == 200
        else:
            opener = build_opener(
                ProxyHandler({}), MeteredHTTPHandler(network_meter=client_meter)
            )
            request = Request(
                f"http://{host}:{port}/private-path",
                data=request_body,
                headers={"Authorization": f"Bearer {token}"},
            )
            with opener.open(request, timeout=2) as response:
                assert response.read() == response_body
    finally:
        thread.join(timeout=5)
        server.server_close()
    assert not thread.is_alive()
    assert hostile_proxy == ([], [])
    client, endpoint = client_meter.snapshot(), server_meter.snapshot()
    assert client["bytes_sent_known"] == endpoint["bytes_received_known"]
    assert client["bytes_received_known"] == endpoint["bytes_sent_known"]
    assert client["bytes_sent_known"] > len(request_body)
    assert client["bytes_received_known"] > len(response_body)
    assert client["connection_count"] == endpoint["connection_count"] == 1
    for evidence in (client, endpoint):
        assert evidence["active_connections"] == 0
        assert evidence["closed_connections"] == 1
        assert evidence["errors"] == []
        assert evidence["send_partial_unknown"] is False
        assert evidence["receive_partial_unknown"] is False
        row = evidence["connections"][0]
        assert row["observation_started_monotonic_ns"] <= row["connected_monotonic_ns"]
        assert row["connected_monotonic_ns"] <= row["closed_monotonic_ns"]
        assert (
            sum(
                event["returned_bytes"]
                for event in row["io_events"]
                if event["operation"] in {"send", "sendall"}
            )
            == row["bytes_sent_known"]
        )
    assert client["connections"][0]["peer"] == {"host": host, "port": port}
    assert endpoint["connections"][0]["peer"] == client["connections"][0]["local"]
    assert endpoint["connections"][0]["local"] == client["connections"][0]["peer"]
    assert validate_network_pair(client, endpoint) == []
    serialized = json.dumps([client, endpoint])
    for secret in (
        token,
        "Bearer",
        "private-path",
        *(body.decode() for body in (request_body, response_body) if body),
    ):
        assert secret not in serialized


def test_direct_socket_methods_and_makefile_do_not_double_count():
    with _tcp_pair() as (left, right):
        lm, rm = SocketNetworkMeter("client"), SocketNetworkMeter("server")
        a, b = lm.wrap(left), rm.wrap(right)
        assert lm.wrap(a) is a
        assert a.send(b"abc") == 3
        assert b.recv(3, socket.MSG_PEEK) == b"abc"
        assert b.recv(3) == b"abc"
        a.sendall(b"defgh")
        buffer = bytearray(5)
        assert b.recv_into(buffer) == 5
        assert bytes(buffer) == b"defgh"
        file = a.makefile("wb")
        file.write(b"ijk")
        file.flush()
        file.close()
        assert b.recv(3) == b"ijk"
        a.close()
        a.close()
        assert b.recv(1) == b""
        b.close()
        assert (
            lm.snapshot()["bytes_sent_known"]
            == rm.snapshot()["bytes_received_known"]
            == 11
        )
        assert lm.snapshot()["closed_connections"] == 1
        assert rm.snapshot()["errors"] == []


def test_file_reference_prevents_reset_until_real_socket_close():
    with _tcp_pair() as (left, right):
        meter = SocketNetworkMeter("client")
        proxy = meter.wrap(left)
        file = proxy.makefile("rb")
        proxy.close()
        assert meter.active_count == 1
        with pytest.raises(RuntimeError, match="active"):
            meter.reset_closed()
        right.sendall(b"ok")
        assert file.read(2) == b"ok"
        file.close()
        assert meter.active_count == 0
        retained = meter.reset_closed()
        assert retained["bytes_received_known"] == 2
        assert retained["closed_connections"] == 1
        assert meter.snapshot()["connection_count"] == 0


def test_failed_sendall_retains_unknown_partial_and_never_claims_requested_length():
    with _tcp_pair() as (left, right):

        class PartialSendFailure:
            def __getattr__(self, name):
                return getattr(left, name)

            def sendall(self, data, flags=0):
                left.sendall(data[:3], flags)
                raise BrokenPipeError("private-partial-error")

        meter = SocketNetworkMeter("client")
        proxy = meter.wrap(PartialSendFailure())
        with pytest.raises(BrokenPipeError):
            proxy.sendall(b"private-payload")
        assert right.recv(3) == b"pri"
        proxy.close()
        evidence = meter.snapshot()
        assert evidence["bytes_sent_known"] == 0
        assert evidence["send_partial_unknown"] is True
        event = evidence["connections"][0]["io_events"][0]
        assert event["returned_bytes"] is None
        assert event["unknown_partial"] is True
        with pytest.raises(RuntimeError, match="retained"):
            meter.reset_closed()
        assert meter.snapshot()["connection_count"] == 1
        assert "private" not in json.dumps(evidence)


def test_read_failure_keeps_prior_success_and_unknown_partial_sanitized():
    with _tcp_pair() as (left, right):
        meter = SocketNetworkMeter("server")
        proxy = meter.wrap(right)
        left.sendall(b"ok")
        assert proxy.recv(2) == b"ok"
        proxy.settimeout(0.01)
        with pytest.raises(TimeoutError):
            proxy.recv_into(bytearray(5))
        proxy.close()
        evidence = meter.snapshot()
        assert evidence["bytes_received_known"] == 2
        assert evidence["receive_partial_unknown"] is True
        assert evidence["connections"][0]["io_events"][-1]["returned_bytes"] is None
        with pytest.raises(RuntimeError, match="retained"):
            meter.reset_closed()


def test_failed_connect_retains_safe_attempt_and_clock():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    host, port = listener.getsockname()
    listener.close()
    meter = SocketNetworkMeter("client")
    connection = MeteredHTTPConnection(host, port, timeout=1, network_meter=meter)
    with pytest.raises(OSError):
        connection.connect()
    evidence = meter.snapshot()
    assert evidence["connection_count"] == 1
    assert evidence["active_connections"] == 0
    row = evidence["connections"][0]
    assert row["connect_result"] == "failed"
    assert row["requested_peer"] == {"host": host, "port": port}
    assert row["closed_monotonic_ns"] >= row["observation_started_monotonic_ns"]
    assert evidence["errors"][0]["operation"] == "connect"


@pytest.fixture(scope="module")
def paired_stream_evidence():
    with _tcp_pair() as (left, right):
        lm, rm = SocketNetworkMeter("client"), SocketNetworkMeter("server")
        a, b = lm.wrap(left), rm.wrap(right)
        a.sendall(b"abc")
        assert b.recv(3) == b"abc"
        b.sendall(b"defgh")
        assert a.recv(5) == b"defgh"
        a.close()
        b.close()
        pair = lm.snapshot(), rm.snapshot()
        assert validate_network_pair(*pair) == []
        return pair


@pytest.mark.parametrize(
    "damage",
    [
        "peer",
        "missing_connection",
        "counter",
        "event_count",
        "partial",
        "active",
        "origin",
        "lifetime",
        "event_clock",
        "extra_connection",
        "summary",
    ],
)
def test_network_pair_rejects_endpoint_byte_clock_and_lifecycle_damage(
    paired_stream_evidence, damage
):
    client, server = deepcopy(paired_stream_evidence)
    row = client["connections"][0]
    if damage == "peer":
        row["peer"]["port"] += 1
    elif damage == "missing_connection":
        server["connections"].clear()
    elif damage == "counter":
        row["bytes_sent_known"] += 1
    elif damage == "event_count":
        row["io_events"][0]["returned_bytes"] += 1
    elif damage == "partial":
        row["send_partial_unknown"] = True
    elif damage == "active":
        row["closed_monotonic_ns"] = None
    elif damage == "origin":
        row["process_id"] += 1
    elif damage == "lifetime":
        row["closed_monotonic_ns"] = row["observation_started_monotonic_ns"] - 1
    elif damage == "event_clock":
        row["io_events"][0]["completed_monotonic_ns"] = row["closed_monotonic_ns"] + 1
    elif damage == "extra_connection":
        client["connections"].append(deepcopy(row))
    else:
        client["bytes_received_known"] += 1
    assert validate_network_pair(client, server)


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit])
def test_partial_send_interrupt_is_retained_before_interrupt_propagates(failure_type):
    with _tcp_pair() as (left, right):

        class InterruptedSend:
            def __getattr__(self, name):
                return getattr(left, name)

            def sendall(self, data, flags=0):
                left.sendall(data[:3], flags)
                raise failure_type("private-interrupt-details")

        meter = SocketNetworkMeter("client")
        proxy = meter.wrap(InterruptedSend())
        with pytest.raises(failure_type):
            proxy.sendall(b"private-data")
        assert right.recv(3) == b"pri"
        proxy.close()
        evidence = meter.snapshot()
        assert evidence["bytes_sent_known"] == 0
        assert evidence["send_partial_unknown"] is True
        assert evidence["errors"][0]["error_type"] == failure_type.__name__
        assert evidence["connections"][0]["io_events"][0]["returned_bytes"] is None
        with pytest.raises(RuntimeError, match="retained"):
            meter.reset_closed()
        assert "private" not in json.dumps(evidence)


def test_close_failure_does_not_claim_real_socket_closed_or_allow_reset():
    with _tcp_pair() as (left, _right):

        class OneCloseFailure:
            failed = False

            def __getattr__(self, name):
                return getattr(left, name)

            def close(self):
                if not self.failed:
                    self.failed = True
                    raise OSError("private-close-failure")
                left.close()

        meter = SocketNetworkMeter("client")
        proxy = meter.wrap(OneCloseFailure())
        with pytest.raises(OSError):
            proxy.close()
        assert meter.active_count == 1
        assert meter.snapshot()["connections"][0]["closed_monotonic_ns"] is None
        with pytest.raises(RuntimeError, match="active"):
            meter.reset_closed()
        proxy.close()
        assert meter.active_count == 0
        with pytest.raises(RuntimeError, match="retained"):
            meter.reset_closed()


@pytest.mark.parametrize("damage", ["clock_null", "count_bool", "operation_list"])
def test_network_pair_malformed_metadata_fails_without_throwing(
    paired_stream_evidence, damage
):
    client, server = deepcopy(paired_stream_evidence)
    if damage == "clock_null":
        client["clock"] = None
    elif damage == "count_bool":
        client["connection_count"] = True
    else:
        client["connections"][0]["io_events"][0]["operation"] = []
    assert validate_network_pair(client, server)


@pytest.mark.parametrize("close_fails", [False, True])
def test_connect_metadata_failure_closes_owned_socket_or_keeps_failed_close_active(
    close_fails,
):
    with _tcp_pair() as (left, right):

        class MetadataFailure:
            def __getattr__(self, name):
                return getattr(left, name)

            def getpeername(self):
                raise OSError("private-metadata-error")

            def close(self):
                if close_fails:
                    raise OSError("private-cleanup-error")
                left.close()

        meter = SocketNetworkMeter("client")
        connection = MeteredHTTPConnection(
            "127.0.0.1", right.getsockname()[1], timeout=1, network_meter=meter
        )
        connection._create_connection = lambda *_args, **_kwargs: MetadataFailure()
        with pytest.raises(OSError):
            connection.connect()
        evidence = meter.snapshot()
        assert evidence["errors"]
        if close_fails:
            assert left.fileno() >= 0
            assert evidence["active_connections"] == 1
            assert evidence["connections"][0]["closed_monotonic_ns"] is None
        else:
            assert left.fileno() == -1
            assert connection.sock is None
            assert evidence["active_connections"] == 0
            assert evidence["closed_connections"] == 1
        with pytest.raises(RuntimeError):
            meter.reset_closed()
        assert "private" not in json.dumps(evidence)
