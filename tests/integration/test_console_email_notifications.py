"""Loopback SMTP controls exercise safe HTTP boundaries without real mail delivery."""

from datetime import datetime, timezone
import http.client
import json
import sqlite3
import threading
from types import SimpleNamespace
from email import policy as email_policy
from email.parser import BytesParser

import pytest

from g15.console.notifications import (
    EmailNotificationController,
    NotificationController,
)
from g15.console.server import ConsoleError, ConsoleHTTPServer
from g15.notifications import AlertSnapshot, NotificationService, ProviderReceipt
from g15.notifications.smtp import EmailNotificationService, SMTP163Transport

SENDER = "g15-synthetic-sender@163.com"
RECIPIENT = "receiver@example.invalid"
AUTH_CODE = "synthetic-smtp-auth-0123456789"
PAIR = ("email-run", "email-device")


class FakeSMTPFactory:
    def __init__(self):
        self.sent = []

    def __call__(self, **options):
        sent = self.sent

        class Client:
            def ehlo(self):
                return 250, b"synthetic hello"

            def login(self, sender, auth_code):
                assert sender == SENDER and auth_code == AUTH_CODE
                return 235, b"synthetic authenticated"

            def sendmail(self, sender, recipients, data):
                options["data_started"].set()
                sent.append((sender, recipients, data))
                return {}

            def close(self):
                pass

        return Client()


class FakeWechat:
    def __init__(self):
        self.calls = []

    def send(self, token, title, content):
        self.calls.append((token, title, content))
        return ProviderReceipt("synthetic-wechat-receipt")


@pytest.fixture
def email_http(tmp_path, monkeypatch):
    monkeypatch.delenv("G15_PUSHPLUS_TOKEN", raising=False)
    report = AlertSnapshot(
        PAIR[0],
        PAIR[1],
        2,
        "red:512",
        {
            "run_id": PAIR[0],
            "device_id": PAIR[1],
            "verifiable": False,
            "passed": None,
            "detection_rate": None,
            "generated_at_utc": "2026-11-13T10:00:00Z",
        },
        "# 完整报告\r\n\r\npassed: null\r\n最后一行。\r\n",
        ("复核振动与温度趋势。", "检查传感器安装。"),
    )
    state = {"upstream": 200, "identity": PAIR, "snapshot_reads": 0}

    def upstream(pair, parameters):
        assert pair == PAIR and parameters == {"page_size": "1"}
        return (
            state["upstream"],
            json.dumps(
                {"run_id": state["identity"][0], "device_id": state["identity"][1]}
            ).encode(),
        )

    def source(pair):
        assert pair == PAIR
        state["snapshot_reads"] += 1
        return report

    def live():
        return {
            "status": "running",
            "interval_seconds": 1,
            "devices": [
                {
                    "run_id": PAIR[0],
                    "device_id": PAIR[1],
                    "last_commit_at_utc": datetime.now(timezone.utc).isoformat(),
                    "error": None,
                }
            ],
        }

    def resource(parameters):
        pair = parameters.get("run_id"), parameters.get("device_id")
        if pair != PAIR:
            raise ConsoleError(404, "not_found")
        return pair

    application = SimpleNamespace(
        allowed={PAIR}, upstream_get=upstream, live_status=live, resource=resource
    )
    factory = FakeSMTPFactory()
    service = EmailNotificationService(
        tmp_path / "email-notifications.sqlite",
        transport=SMTP163Transport(smtp_factory=factory),
    )
    control = EmailNotificationController(
        application, source, tmp_path / "unused-email.sqlite", service=service
    )
    wechat_transport = FakeWechat()
    wechat_service = NotificationService(
        tmp_path / "notifications.sqlite", transport=wechat_transport
    )
    wechat = NotificationController(
        application, source, tmp_path / "unused-wechat.sqlite", service=wechat_service
    )
    application.notifications, application.email_notifications = wechat, control
    server = ConsoleHTTPServer(application, "127.0.0.1", 0)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    def request(path, payload=None, *, headers=None, raw=None, method=None):
        method = method or ("POST" if payload is not None or raw is not None else "GET")
        body = (
            raw
            if raw is not None
            else json.dumps(payload).encode() if payload is not None else None
        )
        defaults = {
            "Origin": origin,
            "Content-Type": "application/json",
            "X-G15-Console-Action": "notifications",
        }
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            conn.request(
                method,
                path,
                body=body,
                headers=defaults if headers is None else headers,
            )
            response = conn.getresponse()
            content = response.read()
            return response.status, json.loads(content) if content else None
        finally:
            conn.close()

    def raw_request(path, body, extra_headers):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            conn.putrequest("POST", path, skip_host=True)
            for name, value in [
                ("Host", f"127.0.0.1:{server.server_port}"),
                ("Origin", origin),
                ("Content-Type", "application/json"),
                ("X-G15-Console-Action", "notifications"),
                ("Content-Length", str(len(body))),
                *extra_headers,
            ]:
                conn.putheader(name, value)
            conn.endheaders(body)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    yield SimpleNamespace(
        request=request,
        raw_request=raw_request,
        control=control,
        service=service,
        smtp=factory,
        state=state,
        origin=origin,
        report=report,
        wechat_service=wechat_service,
        wechat_transport=wechat_transport,
        path=tmp_path,
    )
    server.shutdown()
    thread.join(3)
    server.server_close()
    control.close()
    wechat.close()


def configure(fixture, **changes):
    values = {
        "enabled": False,
        "threshold": 2,
        "cooldown_seconds": 600,
        "sender": SENDER,
        "recipient": RECIPIENT,
        "auth_code": AUTH_CODE,
        "channel": "smtp163",
    }
    values.update(changes)
    return fixture.request("/api/email-notifications/settings", values)


def test_preview_is_read_only_and_explicit_test_contains_complete_attachments(
    email_http,
):
    f = email_http
    assert configure(f)[0] == 200
    status, preview = f.request(
        f"/api/email-notifications/preview?run_id={PAIR[0]}&device_id={PAIR[1]}"
    )
    assert status == 200 and preview["advice"] == list(f.report.advice)
    assert f.report.report_markdown in preview["markdown"]
    attachments = {part["filename"]: part for part in preview["attachments"]}
    assert set(attachments) == {"summary.md", "summary.json"}
    assert attachments["summary.md"]["bytes"] == len(
        f.report.report_markdown.encode("utf-8")
    )
    expected_json = json.dumps(
        dict(f.report.report_json), ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    assert attachments["summary.json"]["bytes"] == len(expected_json)
    assert (
        attachments["summary.json"]["content_type"] == "application/json; charset=utf-8"
    )
    assert not f.smtp.sent and f.service.status()["counts"] == {}
    status, result = f.request(
        "/api/email-notifications/test", {"run_id": PAIR[0], "device_id": PAIR[1]}
    )
    assert status == 202 and result["queued"] is True
    assert not f.smtp.sent
    assert f.service.dispatch_once()["decision"] == "provider_accepted"
    message = BytesParser(policy=email_policy.default).parsebytes(f.smtp.sent[0][2])
    assert "测试通知" in str(message["Subject"])
    actual = {
        part.get_filename(): part.get_payload(decode=True)
        for part in message.iter_attachments()
    }
    assert actual == {
        "summary.md": f.report.report_markdown.encode("utf-8"),
        "summary.json": expected_json,
    }
    status, public = f.request("/api/email-notifications")
    assert status == 200 and public["history"][0]["state"] == "provider_accepted"
    assert public["delivery_scope"] == "smtp_accepted_not_handset_delivery"
    assert public["settings"]["channel"] == "smtp163"
    assert AUTH_CODE not in json.dumps(public, ensure_ascii=False)


def test_smtp_configuration_does_not_modify_or_send_wechat(email_http):
    f = email_http
    assert configure(f, enabled=True)[0] == 200
    f.control.scan_once()
    assert f.service.status()["counts"] == {"queued": 1}
    status, wechat = f.request("/api/notifications")
    assert status == 200 and wechat["settings"]["channel"] == "pushplus"
    assert not wechat["configured"] and not wechat["settings"]["enabled"]
    assert f.wechat_service.status()["counts"] == {} and not f.wechat_transport.calls
    token = "synthetic-wechat-test-token-123456"
    status, _ = f.request(
        "/api/notifications/settings",
        {"enabled": False, "threshold": 1, "cooldown_seconds": 600, "token": token},
    )
    assert status == 200
    assert (
        f.request("/api/notifications/test", {"run_id": PAIR[0], "device_id": PAIR[1]})[
            0
        ]
        == 202
    )
    assert f.wechat_service.dispatch_once()["decision"] == "provider_accepted"
    assert len(f.wechat_transport.calls) == 1 and not f.smtp.sent


@pytest.mark.parametrize(
    "changes",
    [
        {"sender": "first@example.invalid"},
        {"recipient": "a@example.invalid,b@example.invalid"},
        {"sender": SENDER + "\r\nBcc: bad@example.invalid"},
        {"recipient": "名@example.invalid"},
        {"threshold": True},
        {"cooldown_seconds": -1},
        {"auth_code": True},
        {"auth_code": "bad\r\ncode"},
        {"channel": "pushplus"},
        {"host": "attacker.invalid"},
    ],
)
def test_invalid_settings_fail_atomically_without_persisting_credentials(
    email_http, changes
):
    f = email_http
    assert configure(f, **changes)[0] == 400
    status, public = f.request("/api/email-notifications")
    assert status == 200 and not public["configured"]
    assert f.service.status()["counts"] == {} and not f.smtp.sent
    with sqlite3.connect(f.path / "email-notifications.sqlite") as connection:
        persisted = "\n".join(connection.iterdump())
    assert AUTH_CODE not in persisted


@pytest.mark.parametrize(
    "case",
    [
        "missing_origin",
        "wrong_origin",
        "missing_action",
        "wrong_content_type",
        "duplicate_json",
        "unknown_test_field",
    ],
)
def test_write_controls_keep_same_origin_and_strict_json_requirements(email_http, case):
    f = email_http
    assert configure(f)[0] == 200
    headers = {
        "Origin": f.origin,
        "Content-Type": "application/json",
        "X-G15-Console-Action": "notifications",
    }
    body = {"run_id": PAIR[0], "device_id": PAIR[1]}
    raw = None
    if case == "missing_origin":
        headers.pop("Origin")
    elif case == "wrong_origin":
        headers["Origin"] = "https://attacker.invalid"
    elif case == "missing_action":
        headers.pop("X-G15-Console-Action")
    elif case == "wrong_content_type":
        headers["Content-Type"] = "text/plain"
    elif case == "duplicate_json":
        raw = b'{"run_id":"email-run","run_id":"other","device_id":"email-device"}'
    else:
        body["recipient"] = "redirect@example.invalid"
    assert f.request("/api/email-notifications/test", body, headers=headers, raw=raw)[
        0
    ] in (400, 403)
    assert f.service.status()["counts"] == {} and not f.smtp.sent


@pytest.mark.parametrize(
    "extra",
    [
        [("Origin", "https://attacker.invalid")],
        [("Host", "attacker.invalid")],
        [("Content-Length", "1")],
        [("Transfer-Encoding", "chunked")],
        [("Expect", "100-continue")],
    ],
)
def test_duplicate_or_ambiguous_request_headers_cannot_queue_mail(email_http, extra):
    f = email_http
    assert configure(f)[0] == 200
    body = json.dumps({"run_id": PAIR[0], "device_id": PAIR[1]}).encode()
    assert f.raw_request("/api/email-notifications/test", body, extra)[0] in (400, 403)
    assert f.service.status()["counts"] == {} and not f.smtp.sent


def test_unconfigured_or_unauthorized_resources_never_read_or_queue_a_report(
    email_http,
):
    f = email_http
    request_body = {"run_id": PAIR[0], "device_id": PAIR[1]}
    assert f.request("/api/email-notifications/test", request_body)[0] == 400
    assert f.state["snapshot_reads"] == 0
    assert configure(f)[0] == 200
    assert (
        f.request(
            "/api/email-notifications/test", {"run_id": PAIR[0], "device_id": "other"}
        )[0]
        == 404
    )
    assert f.state["snapshot_reads"] == 0
    f.state["upstream"] = 401
    assert f.request("/api/email-notifications/test", request_body)[0] == 401
    assert f.state["snapshot_reads"] == 0
    f.state["upstream"], f.state["identity"] = 200, ("wrong-run", PAIR[1])
    assert f.request("/api/email-notifications/test", request_body)[0] == 502
    assert f.state["snapshot_reads"] == 0
    assert f.service.status()["counts"] == {} and not f.smtp.sent


def test_omitted_auth_code_keeps_only_the_same_sender_and_empty_code_clears(email_http):
    f = email_http
    assert configure(f)[0] == 200
    values = {
        "enabled": False,
        "threshold": 2,
        "cooldown_seconds": 600,
        "sender": SENDER,
        "recipient": "other@example.invalid",
    }
    status, public = f.request("/api/email-notifications/settings", values)
    assert status == 200 and public["configured"]
    values["sender"] = "g15-other-synthetic-sender@163.com"
    status, public = f.request("/api/email-notifications/settings", values)
    assert status == 400
    status, public = f.request("/api/email-notifications")
    assert public["configured"] and public["settings"]["sender"] == SENDER
    values["auth_code"] = ""
    status, public = f.request("/api/email-notifications/settings", values)
    assert status == 200 and not public["configured"]
    assert configure(f)[0] == 200
    status, public = configure(f, auth_code="")
    assert status == 200 and not public["configured"]
    assert configure(f, enabled=True, auth_code="")[0] == 400


def test_queue_failure_is_not_reported_as_an_accepted_test(email_http, monkeypatch):
    f = email_http
    assert configure(f)[0] == 200
    monkeypatch.setattr(
        f.service, "enqueue_test", lambda _snapshot: {"decision": "outbox_full"}
    )
    status, result = f.request(
        "/api/email-notifications/test", {"run_id": PAIR[0], "device_id": PAIR[1]}
    )
    assert status == 503 and result["error"] == "notification_outbox_full"
    assert not f.smtp.sent
