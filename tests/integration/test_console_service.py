"""Real authenticated upstream HTTP plus the separate read-only console boundary."""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import secrets
import threading
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from g15.console import server as console
from g15.query.application import HealthQueryApplication
from g15.query.auth import AuthorizationPolicy, TokenVerifier, issue_token
from g15.query.cursor import CursorCodec
from g15.service import http as transport
from tools.query_load.d0_fixture import (
    build_d0_runs,
    build_policy_yaml,
    device_id,
    device_run_id,
)

# A completed response (especially HEAD) can precede worker socket cleanup.
# Ordinary route checks allow that lifecycle overlap; overload tests reserve
# every configured permit explicitly instead of relying on scheduling.
FIXTURE_WORKERS = 4


@pytest.fixture(scope="module", name="public_runs")
def _public_runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("console-public-d0") / "runs"
    build_d0_runs(root)
    return root


@pytest.fixture(name="service")
def _service(tmp_path, public_runs):
    bearer = secrets.token_bytes(32)
    calls, sent = [], []
    application = HealthQueryApplication(
        runs_root=public_runs,
        token_verifier=TokenVerifier(
            key=bearer,
            issuer="console-test",
            audience="console-test",
            clock=lambda: 2000,
        ),
        authorization_policy=AuthorizationPolicy.from_yaml(build_policy_yaml()),
        cursor_codec=CursorCodec(secrets.token_bytes(32)),
        event_sink=lambda _event: None,
    )

    class RecordedHandler(transport.QueryRequestHandler):
        def do_GET(self):
            calls.append(
                {
                    "target": self.path,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            super().do_GET()

        def _send_json(self, status, body, **kwargs):
            sent.append((int(status), transport._json_payload(body)))
            return super()._send_json(status, body, **kwargs)

    upstream = transport.create_query_server(
        application, max_workers=FIXTURE_WORKERS, event_sink=lambda _event: None
    )
    upstream.RequestHandlerClass = RecordedHandler
    upstream_thread = threading.Thread(
        target=lambda: upstream.serve_forever(poll_interval=0.01), daemon=True
    )
    upstream_thread.start()
    run_id, device = device_run_id(0), device_id(0)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "run_id": run_id,
                    "device_id": device,
                    "scenario": "degradation",
                    "total_points": 1000,
                    "token": "catalog-private-token",
                    "path": "private-file-path",
                }
            ]
        )
    )
    reports_root = tmp_path / "report-runs"
    reports = reports_root / run_id / "reports"
    reports.mkdir(parents=True)
    report = {
        "run_id": run_id,
        "device_id": device,
        "verifiable": False,
        "passed": None,
        "false_alarm_count": 9,
    }
    report_bytes = json.dumps(report, indent=2).encode("utf-8")
    (reports / "summary.json").write_bytes(report_bytes)
    (reports / "summary.md").write_text(
        f"# Public report\n\n| run_id | `{run_id}` |\n| device_id | `{device}` |\n\nUndetermined.\n",
        encoding="utf-8",
    )
    token_file = tmp_path / "token.txt"
    token = issue_token(
        key=bearer,
        issuer="console-test",
        audience="console-test",
        subject="subject-d0",
        now=1900,
    )
    token_file.write_text(token + "\n", encoding="ascii")
    host, port = upstream.server_address
    server = console.create_console_server(
        catalog,
        reports_root,
        f"http://{host}:{port}",
        token_file,
        max_workers=FIXTURE_WORKERS,
    )
    assets = tmp_path / "test-assets"
    assets.mkdir()
    for name in ("index.html", "app.js", "app.css", "state.js"):
        (assets / name).write_text("console-fixture", encoding="utf-8")
    (assets / "secret.txt").write_text("private-static-file", encoding="utf-8")
    server.application.asset_root = assets
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    harness = SimpleNamespace(
        server=server,
        upstream=upstream,
        worker_count=FIXTURE_WORKERS,
        calls=calls,
        sent=sent,
        token=token,
        token_file=token_file,
        catalog=catalog,
        reports_root=reports_root,
        reports=reports,
        report_bytes=report_bytes,
        pair={"run_id": run_id, "device_id": device},
        tmp_path=tmp_path,
    )
    try:
        yield harness
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)
        assert not thread.is_alive() and not upstream_thread.is_alive()


def _request(service, path, *, method="GET", headers=None):
    host, port = service.server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _path(service, route, **extra):
    return route + "?" + urlencode({**service.pair, **extra})


def test_catalog_assets_head_and_secret_redaction(service, caplog):
    status, headers, payload = _request(service, "/api/catalog")
    assert status == 200
    data = json.loads(payload)
    assert data["readonly"] is True
    assert data["devices"] == [
        {**service.pair, "scenario": "degradation", "total_points": 1000}
    ]
    assert service.calls == []
    assert "unsafe-inline" not in headers["Content-Security-Policy"]
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers
    for route in ("/", "/assets/app.css", "/assets/app.js", "/assets/state.js"):
        assert _request(service, route)[0] == 200
        code, head_headers, body = _request(service, route, method="HEAD")
        assert code == 200 and body == b""
        assert int(head_headers["Content-Length"]) == len(b"console-fixture")
    for route in (
        "/assets/secret.txt",
        "/assets/../secret.txt",
        "/assets/%2e%2e/token.txt",
        "/private_truth/faults.json",
    ):
        assert _request(service, route)[0] == 404
    for secret in (
        service.token,
        "catalog-private-token",
        "private-file-path",
        "private-static-file",
    ):
        assert secret not in payload.decode() + caplog.text


def test_health_is_exact_real_http_json_with_server_side_bearer(service):
    status, _headers, payload = _request(
        service, _path(service, "/api/health", page_size="2")
    )
    assert status == 200
    assert (status, payload) == service.sent[-1]
    assert len(json.loads(payload)["windows"]["items"]) == 2
    assert service.calls[-1]["authorization"] == "Bearer " + service.token
    assert service.token not in service.calls[-1]["target"]
    next_cursor = json.loads(payload)["windows"]["next_cursor"]
    assert (
        _request(
            service,
            _path(service, "/api/health", page_size="2", windows_cursor=next_cursor),
        )[0]
        == 200
    )


def test_reports_probe_real_authorization_then_preserve_public_summary(service):
    for route, fmt, expected, mime in [
        ("/api/report", None, service.report_bytes, "application/json"),
        ("/api/report/download", "json", service.report_bytes, "application/json"),
        (
            "/api/report/download",
            "markdown",
            (service.reports / "summary.md").read_bytes(),
            "text/markdown",
        ),
    ]:
        path = _path(service, route, **({"format": fmt} if fmt else {}))
        status, headers, payload = _request(service, path)
        assert status == 200 and payload == expected
        assert headers["Content-Type"].startswith(mime)
        assert "page_size=1" in service.calls[-1]["target"]
        if fmt:
            assert headers["Content-Disposition"].startswith("attachment;")
    summary = json.loads(_request(service, _path(service, "/api/report"))[2])
    assert summary["verifiable"] is False and summary["passed"] is None
    assert summary["false_alarm_count"] == 9


def test_rotated_invalid_token_preserves_401_and_never_opens_report(
    service, monkeypatch, caplog
):
    service.token_file.write_text("invalid-token", encoding="ascii")

    def forbidden_file_access(_path):
        pytest.fail("report path must not be inspected before successful authorization")

    monkeypatch.setattr(console, "_reparse", forbidden_file_access)
    status, headers, payload = _request(service, _path(service, "/api/report"))
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    assert (status, payload) == service.sent[-1]
    service.token_file.write_text(service.token, encoding="ascii")
    assert _request(service, _path(service, "/api/health"))[0] == 200
    assert service.token not in caplog.text and "invalid-token" not in payload.decode()


@contextmanager
def _occupy_all_workers(semaphore, count):
    """Wait for cleanup, then hold the complete configured pool until release."""
    acquired = 0
    try:
        for _index in range(count):
            assert semaphore.acquire(timeout=2), "previous worker failed to release"
            acquired += 1
        if semaphore.acquire(blocking=False):
            acquired += 1
            pytest.fail("worker pool exceeds its configured permit count")
        yield
    finally:
        for _index in range(acquired):
            semaphore.release()


def test_upstream_404_and_503_keep_their_semantics(service):
    missing = ("missing-run", service.pair["device_id"])
    service.server.application.allowed.add(missing)
    status, _headers, payload = _request(
        service,
        "/api/health?" + urlencode({"run_id": missing[0], "device_id": missing[1]}),
    )
    assert status == 404 and (status, payload) == service.sent[-1]
    with _occupy_all_workers(service.upstream._worker_slots, service.worker_count):
        assert _request(service, _path(service, "/api/health"))[0] == 503
        assert _request(service, _path(service, "/api/report"))[0] == 503
    assert _request(service, _path(service, "/api/health"))[0] == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example:18467"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_host_origin_fetch_site_reject_cross_site_without_upstream(service, headers):
    status, _headers, _payload = _request(
        service, _path(service, "/api/health"), headers=headers
    )
    assert status == 403 and service.calls == []


def test_same_origin_get_and_readonly_method_framing(service):
    host, port = service.server.server_address[:2]
    headers = {"Origin": f"http://{host}:{port}", "Sec-Fetch-Site": "same-origin"}
    assert _request(service, "/api/catalog", headers=headers)[0] == 200
    assert _request(service, "/api/catalog", method="POST")[0] == 405
    assert _request(service, "/api/catalog", headers={"Content-Length": "1"})[0] == 400
    assert (
        _request(service, "/api/catalog", headers={"Expect": "100-continue"})[0] == 400
    )
    assert _request(service, "/" + "x" * 9000)[0] == 414
    assert service.calls == []


def test_browser_cannot_choose_url_path_or_unlisted_pair(service):
    routes = [
        _path(service, "/api/health", upstream="http://evil.example"),
        _path(service, "/api/report", path="../token.txt"),
        _path(service, "/api/report/download", format="../token.txt"),
        "/api/health?run_id=../private_truth&device_id=private",
        _path(service, "/api/health") + "&run_id=duplicate",
    ]
    assert [_request(service, route)[0] for route in routes] == [
        400,
        400,
        400,
        404,
        400,
    ]
    assert service.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1",
        "http://example.com",
        "http://127.0.0.1:1/path",
        "http://127.0.0.1:1?query=x",
        "http://user:pass@127.0.0.1:1",
        "http://127.0.0.1:1#fragment",
    ],
)
def test_upstream_origin_rejects_non_loopback_or_extra_url_components(url):
    with pytest.raises(ValueError):
        console._upstream_address(url)


@pytest.mark.parametrize("part", ["file", "reports", "run"])
def test_report_symlink_escape_is_rejected_after_authorization(service, part):
    outside = service.tmp_path / "outside"
    outside.mkdir()
    (outside / "summary.json").write_text('{"private":"never-return"}')
    if part == "file":
        location, target = service.reports / "summary.json", outside / "summary.json"
        location.unlink()
    elif part == "reports":
        location, target = service.reports, outside
        location.rename(service.tmp_path / "old-reports")
    else:
        location, target = service.reports.parent, outside
        location.rename(service.tmp_path / "old-run")
    try:
        location.symlink_to(target, target_is_directory=part != "file")
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {type(exc).__name__}")
    status, _headers, payload = _request(service, _path(service, "/api/report"))
    assert status == 404 and b"never-return" not in payload
    assert "page_size=1" in service.calls[-1]["target"]


def test_report_file_swap_is_rejected_before_reading_replaced_inode(
    service, monkeypatch
):
    path = service.reports / "summary.json"
    outside = service.tmp_path / "outside.json"
    outside.write_text('{"private":"never-return"}')
    original_open = console.os.open

    def swap_before_open(filename, flags):
        if Path(filename) == path:
            path.unlink()
            os.link(outside, path)
        return original_open(filename, flags)

    monkeypatch.setattr(console.os, "open", swap_before_open)
    status, _headers, payload = _request(service, _path(service, "/api/report"))
    assert status == 404 and b"never-return" not in payload


def test_local_worker_bound_returns_503_without_upstream(service):
    with _occupy_all_workers(service.server.worker_slots, service.worker_count):
        assert _request(service, "/api/catalog")[0] == 503
    assert _request(service, "/api/catalog")[0] == 200
    assert service.calls == []


def test_empty_catalog_and_private_metadata_are_not_an_error(service):
    empty = service.tmp_path / "empty.json"
    empty.write_text("[]")
    app = console.ConsoleApplication(
        empty, service.reports_root, "http://127.0.0.1:1", service.token_file
    )
    assert app.devices == [] and app.allowed == set()


def _replace_upstream(service, callback):
    """Use the existing temporary listener to exercise malformed upstream data."""
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            callback(self)

        def log_message(self, _format, *args):
            del args

    service.upstream.RequestHandlerClass = Handler


def _send_upstream(handler, status, payload, **headers):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Connection", "close")
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(payload)
    handler.close_connection = True


@pytest.mark.parametrize("format_name", ["json", "markdown"])
def test_misfiled_report_cannot_cross_the_authorized_resource(service, format_name):
    if format_name == "json":
        (service.reports / "summary.json").write_text(
            '{"run_id":"other-run","device_id":"other-device","private":"not-this-resource"}'
        )
    else:
        (service.reports / "summary.md").write_text(
            "| run_id | `other-run` |\n| device_id | `other-device` |\nprivate-report"
        )
    status, _headers, payload = _request(
        service, _path(service, "/api/report/download", format=format_name)
    )
    assert status == 502 and json.loads(payload)["error"] == "invalid_report"
    assert b"other-run" not in payload and b"private-report" not in payload
    assert "page_size=1" in service.calls[-1]["target"]


def test_unexpected_error_body_cannot_reflect_credentials_paths_or_tracebacks(
    service, caplog
):
    payload = json.dumps(
        {
            "error": "internal",
            "request_id": "safe-request",
            "trace_id": "safe-request",
            "credential": service.token,
            "path": "/private/token.txt",
            "traceback": "private failure",
        }
    ).encode()
    _replace_upstream(service, lambda handler: _send_upstream(handler, 500, payload))
    status, _headers, body = _request(service, _path(service, "/api/health"))
    assert status == 502
    for secret in (service.token, "/private/token.txt", "private failure"):
        assert secret not in body.decode() + caplog.text


@pytest.mark.parametrize("escaped", [False, True])
def test_even_successful_shape_cannot_reflect_current_bearer(service, escaped):
    payload = json.dumps({**service.pair, "unexpected": service.token}).encode()
    if escaped:
        encoded = "".join(
            f"\\u{ord(character):04x}" for character in service.token
        ).encode()
        payload = payload.replace(service.token.encode(), encoded)
    _replace_upstream(service, lambda handler: _send_upstream(handler, 200, payload))
    status, _headers, body = _request(service, _path(service, "/api/health"))
    assert status == 502 and service.token.encode() not in body


def test_success_for_a_different_resource_is_not_forwarded(service):
    _replace_upstream(
        service,
        lambda handler: _send_upstream(
            handler, 200, b'{"run_id":"other","device_id":"other"}'
        ),
    )
    assert _request(service, _path(service, "/api/health"))[0] == 502


def test_redirect_is_not_followed_or_given_a_bearer(service):
    targets = []

    def redirected(handler):
        targets.append(handler.path)
        _send_upstream(
            handler, 302, b"{}", Location="http://127.0.0.1:1/private-target"
        )

    _replace_upstream(service, redirected)
    assert _request(service, _path(service, "/api/health"))[0] == 502
    assert len(targets) == 1 and "private-target" not in targets[0]


def test_upstream_response_size_is_bounded(service, monkeypatch):
    monkeypatch.setattr(console, "MAX_RESPONSE_BYTES", 1024)
    payload = json.dumps({**service.pair, "large": "x" * 2048}).encode()
    _replace_upstream(service, lambda handler: _send_upstream(handler, 200, payload))
    assert _request(service, _path(service, "/api/health"))[0] == 502


def test_slow_trickle_cannot_extend_the_upstream_total_deadline(service):
    import time

    def trickled(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.send_header("Connection", "close")
        handler.end_headers()
        try:
            for _index in range(100):
                handler.wfile.write(b"x")
                time.sleep(0.03)
        except OSError:
            pass
        handler.close_connection = True

    service.server.application.timeout = 0.08
    _replace_upstream(service, trickled)
    started = time.monotonic()
    assert _request(service, _path(service, "/api/health"))[0] == 503
    assert time.monotonic() - started < 0.8


def test_duplicate_host_is_rejected_before_any_upstream(service):
    host, port = service.server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=3)
    try:
        connection.putrequest("GET", "/api/catalog", skip_host=True)
        connection.putheader("Host", f"{host}:{port}")
        connection.putheader("Host", "evil.example")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 403
        response.read()
    finally:
        connection.close()
    assert service.calls == []


def test_explicit_ui_deep_link_can_reload_without_reflecting_its_values(service):
    status, _headers, payload = _request(
        service,
        "/?"
        + urlencode(
            {
                "device": service.pair["device_id"],
                "run": service.pair["run_id"],
                "view": "health",
            }
        ),
    )
    assert status == 200 and payload == b"console-fixture"
    for query in ("view=alarms", "view=reports"):
        assert _request(service, "/?" + query)[0] == 200
    assert _request(service, "/?unknown=x")[0] == 400
    assert _request(service, "/?view=invalid")[0] == 400
    assert _request(service, "/assets/app.js?view=health")[0] == 400
    assert service.calls == []


@pytest.mark.parametrize("workers", [True, 1.0, 1.5, float("nan"), "1", 0, -1, 65])
def test_non_integer_or_out_of_bounds_worker_limits_never_create_a_semaphore(
    service, workers
):
    with pytest.raises(ValueError):
        console.ConsoleHTTPServer(
            service.server.application, "127.0.0.1", 0, max_workers=workers
        )


def test_explicit_upstream_port_zero_is_not_silently_changed_to_http_default():
    with pytest.raises(ValueError):
        console._upstream_address("http://127.0.0.1:0")
