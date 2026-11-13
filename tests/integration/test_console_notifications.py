"""Real loopback HTTP controls, safe disclosure and committed notification flow."""

from datetime import datetime, timedelta, timezone
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from g15.console.server import ConsoleHTTPServer
from g15.console.notifications import NotificationController
from g15.notifications import AlertSnapshot, NotificationService, ProviderReceipt

TOKEN = "synthetic-phone-token-0123456789"
PAIR = ("run-notify", "device-01")


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def send(self, token, title, content):
        self.calls.append((token, title, content))
        return ProviderReceipt("synthetic-receipt")


@pytest.fixture
def notification_http(tmp_path, monkeypatch):
    monkeypatch.delenv("G15_PUSHPLUS_TOKEN", raising=False)
    snapshot = AlertSnapshot(
        PAIR[0],
        PAIR[1],
        2,
        "red:20",
        {
            "run_id": PAIR[0],
            "device_id": PAIR[1],
            "verifiable": False,
            "passed": None,
            "generated_at_utc": "2026-11-13T10:00:00Z",
        },
        "# Complete actual report\npassed: null\nlast line preserved",
        ("复核传感器安装。",),
    )
    state = {"status": "running", "age": 0, "upstream": 200, "provider_calls": 0}

    def live():
        stamp = datetime.now(timezone.utc) - timedelta(seconds=state["age"])
        return {
            "status": state["status"],
            "interval_seconds": 1,
            "devices": [
                {
                    "run_id": PAIR[0],
                    "device_id": PAIR[1],
                    "last_commit_at_utc": stamp.isoformat(),
                    "error": None,
                }
            ],
        }

    def upstream(pair, parameters):
        assert pair == PAIR and parameters == {"page_size": "1"}
        return (
            state["upstream"],
            json.dumps({"run_id": PAIR[0], "device_id": PAIR[1]}).encode(),
        )

    def provider(pair):
        assert pair == PAIR
        state["provider_calls"] += 1
        return snapshot

    transport = RecordingTransport()
    application = SimpleNamespace(
        allowed={PAIR}, upstream_get=upstream, live_status=live
    )
    service = NotificationService(
        tmp_path / "notifications.sqlite", transport=transport
    )
    control = NotificationController(
        application, provider, tmp_path / "unused.sqlite", service=service
    )
    application.notifications = control

    def resource(parameters):
        from g15.console.server import ConsoleError

        pair = parameters.get("run_id"), parameters.get("device_id")
        if pair != PAIR:
            raise ConsoleError(404, "not_found")
        return pair

    application.resource = resource
    server = ConsoleHTTPServer(application, "127.0.0.1", 0)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    def request(path, payload=None, *, headers=None, raw=None):
        method = "GET" if payload is None and raw is None else "POST"
        body = (
            raw
            if raw is not None
            else json.dumps(payload).encode() if payload is not None else None
        )
        defaults = {
            "Origin": origin,
            "X-G15-Console-Action": "notifications",
            "Content-Type": "application/json",
        }
        if headers is not None:
            defaults = headers
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=defaults)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    yield SimpleNamespace(
        request=request,
        control=control,
        service=service,
        transport=transport,
        state=state,
        origin=origin,
        server=server,
    )
    server.shutdown()
    thread.join(3)
    server.server_close()
    control.close()


def configure(fixture, **updates):
    value = {"enabled": True, "threshold": 2, "cooldown_seconds": 0, "token": TOKEN}
    value.update(updates)
    return fixture.request("/api/notifications/settings", value)


def test_real_http_source_to_outbox_to_provider_is_deduplicated(notification_http):
    f = notification_http
    assert configure(f)[0] == 200
    f.control.scan_once()
    f.control.scan_once()
    assert f.service.status()["counts"] == {"queued": 1}
    assert f.service.dispatch_once()["decision"] == "provider_accepted"
    assert len(f.transport.calls) == 1
    assert "last line preserved" in f.transport.calls[0][2]
    assert "复核传感器安装。" in f.transport.calls[0][2]
    status, body = f.request("/api/notifications")
    assert status == 200 and body["history"][0]["threshold"] == 2
    assert body["history"][0]["created_at"].startswith("20")
    assert TOKEN not in json.dumps(body)
    assert body["delivery_scope"] == "provider_accepted_not_handset_delivery"


def test_preview_does_not_enqueue_and_test_requires_explicit_post(notification_http):
    f = notification_http
    assert configure(f, enabled=False)[0] == 200
    status, body = f.request(
        "/api/notifications/preview?run_id=run-notify&device_id=device-01"
    )
    assert status == 200 and "last line preserved" in body["markdown"]
    assert f.service.status()["counts"] == {} and not f.transport.calls
    status, _ = f.request(
        "/api/notifications/test", {"run_id": PAIR[0], "device_id": PAIR[1]}
    )
    assert status == 202
    assert f.service.dispatch_once()["decision"] == "provider_accepted"
    assert "测试通知" in f.transport.calls[0][1]


@pytest.mark.parametrize(
    "case",
    [
        "missing_origin",
        "wrong_origin",
        "missing_action",
        "form",
        "duplicate_json",
        "unknown_field",
        "bool_threshold",
    ],
)
def test_write_actions_fail_closed(notification_http, case):
    f = notification_http
    headers = {
        "Origin": f.origin,
        "X-G15-Console-Action": "notifications",
        "Content-Type": "application/json",
    }
    value = {"enabled": True, "threshold": 1, "cooldown_seconds": 0, "token": TOKEN}
    if case == "missing_origin":
        headers.pop("Origin")
    if case == "wrong_origin":
        headers["Origin"] = "https://other.example"
    if case == "missing_action":
        headers.pop("X-G15-Console-Action")
    if case == "form":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if case == "unknown_field":
        value["callbackUrl"] = "https://other.example"
    if case == "bool_threshold":
        value["threshold"] = True
    raw = (
        b'{"enabled":false,"enabled":true,"threshold":1,"cooldown_seconds":0}'
        if case == "duplicate_json"
        else json.dumps(value).encode()
    )
    status, _ = f.request("/api/notifications/settings", headers=headers, raw=raw)
    assert status in (400, 403)
    assert not f.service.status()["configured"] and not f.transport.calls


def test_authorization_failure_never_reads_snapshot_or_sends(notification_http):
    f = notification_http
    assert configure(f)[0] == 200
    f.state["upstream"] = 403
    status, _ = f.request(
        "/api/notifications/test", {"run_id": PAIR[0], "device_id": PAIR[1]}
    )
    assert status == 403 and f.state["provider_calls"] == 0
    assert not f.service.status()["counts"]


def test_stale_source_does_not_send_or_claim_monitoring(notification_http):
    f = notification_http
    assert configure(f)[0] == 200
    f.state["age"] = 60
    f.control.scan_once()
    assert not f.service.status()["counts"]
    assert f.control.status()["status"]["state"] == "error"
    f.state.update(age=0, status="stopped")
    assert f.control.status()["status"]["state"] == "error"
    f.control.scan_once()
    assert f.control.status()["status"]["state"] == "source_idle"


@pytest.mark.parametrize(
    "decision,expected",
    [("outbox_full", 503), ("content_too_large", 413), ("token_missing", 400)],
)
def test_explicit_test_never_claims_queued_when_enqueue_rejected(
    notification_http, monkeypatch, decision, expected
):
    f = notification_http
    assert configure(f, enabled=False)[0] == 200
    monkeypatch.setattr(
        f.service, "enqueue_test", lambda _snapshot: {"decision": decision}
    )
    status, body = f.request(
        "/api/notifications/test", {"run_id": PAIR[0], "device_id": PAIR[1]}
    )
    assert status == expected
    assert body.get("queued") is not True and not f.service.status()["counts"]


def test_terminal_committed_snapshot_remains_eligible_after_heartbeat_age(
    notification_http,
):
    f = notification_http
    assert configure(f)[0] == 200
    f.state.update(status="stopped", age=600)
    f.control.scan_once()
    assert f.service.status()["counts"] == {"queued": 1}
    assert f.control.status()["status"]["state"] == "source_idle"


def test_shutdown_drains_then_durably_captures_final_red_snapshot(
    notification_http, tmp_path
):
    from g15.live.__main__ import _finish_live_services

    f = notification_http
    assert configure(f)[0] == 200
    f.state["age"] = 60
    f.control.scan_once()
    assert not f.service.status()["counts"]

    class FinalRuntime:
        session_root = tmp_path

        def stop(self, timeout):
            assert timeout == 30
            f.state.update(status="stopped", age=0)
            return {"status": "stopped"}

    assert _finish_live_services(FinalRuntime(), f.control) is False
    assert f.service.status()["counts"] == {"queued": 1}
    assert f.control.status()["status"]["state"] == "source_idle"
