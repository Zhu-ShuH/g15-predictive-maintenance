"""SMTP boundaries use synthetic accounts, fake clients and a real durable outbox."""

from dataclasses import replace
from email import policy as email_policy
from email.parser import BytesParser
import json
import smtplib
import sqlite3
import ssl
import threading
import time

import pytest

from g15.notifications import AlertSnapshot, NotificationPolicy, ProviderError
from g15.notifications.smtp import (
    EmailNotificationService,
    SMTP163Transport,
    validate_email_address,
)
from g15.notifications import smtp

SENDER = "g15-synthetic-sender@163.com"
OTHER_SENDER = "g15-other-synthetic-sender@163.com"
RECIPIENT = "first@example.invalid"
OTHER_RECIPIENT = "second@example.invalid"
AUTH_CODE = "synthetic-auth-code-0123456789"
OTHER_AUTH_CODE = "synthetic-auth-code-9876543210"
HANDLE = "synthetic-credential-handle-012345"


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def snapshot(count=1, **changes):
    values = {
        "run_id": "smtp-run",
        "device_id": "smtp-device",
        "red_count": count,
        "last_red_id": f"red:{count * 256}",
        "report_json": {
            "run_id": "smtp-run",
            "device_id": "smtp-device",
            "generated_at_utc": "2026-11-13T10:00:00Z",
            "verifiable": False,
            "passed": None,
            "detection_rate": None,
            "note": "完整中文报告",
        },
        "report_markdown": "# 完整报告\n\npassed: null\n保留最后一行。\n",
        "advice": ("检查传感器安装。", "复核温度与振动趋势。"),
    }
    values.update(changes)
    return AlertSnapshot(**values)


def envelope(**changes):
    report = snapshot()
    values = {
        "schema_version": "1",
        "message_id": "<0123456789abcdef0123456789abcdef@g15.local>",
        "sender": SENDER,
        "recipient": RECIPIENT,
        "body": "维护建议\n检查传感器安装。\n完整报告在正文和两个附件中。",
        "report_markdown": report.report_markdown,
        "report_json": json.dumps(
            dict(report.report_json),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ),
    }
    values.update(changes)
    return json.dumps(values, ensure_ascii=False)


class FakeSMTP:
    def __init__(self, options, *, login_error=None, send_error=None, refusals=None):
        self.options = options
        self.login_error, self.send_error = login_error, send_error
        self.refusals = refusals or {}
        self.logins, self.sent = [], []
        self.closed = False

    def ehlo(self):
        return 250, b"synthetic hello"

    def login(self, sender, auth_code):
        self.logins.append((sender, auth_code))
        if self.login_error:
            raise self.login_error
        return 235, b"synthetic authenticated"

    def sendmail(self, sender, recipients, data):
        self.options["data_started"].set()
        self.sent.append((sender, recipients, data))
        if self.send_error:
            raise self.send_error
        return self.refusals

    def close(self):
        self.closed = True


class FakeSMTPFactory:
    def __init__(self, **client_options):
        self.clients = []
        self.client_options = client_options

    def __call__(self, **options):
        assert set(options) == {
            "deadline",
            "cancelled",
            "register_socket",
            "data_started",
        }
        assert options["deadline"] > time.monotonic()
        client = FakeSMTP(options, **self.client_options)
        self.clients.append(client)
        return client

    @property
    def sent(self):
        return [message for client in self.clients for message in client.sent]


def engine(tmp_path, *, clock=None, policy=None, factory=None):
    factory = factory or FakeSMTPFactory()
    service = EmailNotificationService(
        tmp_path / "email-notifications.sqlite",
        policy or NotificationPolicy(enabled=True, cooldown_seconds=0),
        transport=SMTP163Transport(smtp_factory=factory),
        clock=clock or Clock(),
    )
    service.configure_email(
        service.policy, sender=SENDER, recipient=RECIPIENT, auth_code=AUTH_CODE
    )
    return service, factory


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        3,
        "",
        "first@example.invalid,second@example.invalid",
        "first@example.invalid;second@example.invalid",
        "Name <first@example.invalid>",
        "first@example.invalid\r\nBcc: second@example.invalid",
        "first@example.invalid\n",
        " first@example.invalid",
        "用户@example.invalid",
        "first@localhost",
        "a" * 255 + "@example.invalid",
    ],
)
def test_single_ascii_recipient_rejects_injection_and_ambiguous_addresses(value):
    with pytest.raises(ValueError):
        validate_email_address(value)


@pytest.mark.parametrize(
    "value", ["first@example.invalid", "first@126.com", "a@163.com.evil.invalid"]
)
def test_sender_must_be_a_single_163_address(value):
    with pytest.raises(ValueError):
        validate_email_address(value, sender=True)


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_transport_preserves_unicode_body_and_both_complete_report_attachments(newline):
    factory = FakeSMTPFactory()
    adapter = SMTP163Transport(smtp_factory=factory)
    adapter.register_credentials(HANDLE, SENDER, AUTH_CODE)
    content = envelope(
        report_markdown=newline.join(["# 完整报告", "", "保留最后一行。", ""])
    )
    adapter.send(HANDLE, "G15 维护建议和完整报告", content)
    sender, recipients, wire = factory.sent[0]
    assert sender == SENDER and recipients == [RECIPIENT]
    message = BytesParser(policy=email_policy.default).parsebytes(wire)
    assert str(message["From"]) == SENDER and str(message["To"]) == RECIPIENT
    assert str(message["Subject"]) == "G15 维护建议和完整报告"
    assert not message.get_all("Bcc") and not message.get_all("Cc")
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "检查传感器安装。" in body
    attachments = {part.get_filename(): part for part in message.iter_attachments()}
    assert set(attachments) == {"summary.md", "summary.json"}
    expected = json.loads(content)
    assert str(message["Message-ID"]) == expected["message_id"]
    assert attachments["summary.md"].get_payload(decode=True) == expected[
        "report_markdown"
    ].encode("utf-8")
    assert attachments["summary.json"].get_payload(decode=True) == expected[
        "report_json"
    ].encode("utf-8")
    assert (
        json.loads(attachments["summary.json"].get_payload(decode=True))["passed"]
        is None
    )
    assert AUTH_CODE.encode() not in wire and HANDLE.encode() not in wire
    assert factory.clients[0].logins == [(SENDER, AUTH_CODE)]
    assert factory.clients[0].closed


@pytest.mark.parametrize(
    "title", ["x\r\nBcc: hidden@example.invalid", "x\nInjected: yes"]
)
def test_header_injection_is_rejected_before_smtp(title):
    factory = FakeSMTPFactory()
    adapter = SMTP163Transport(smtp_factory=factory)
    adapter.register_credentials(HANDLE, SENDER, AUTH_CODE)
    with pytest.raises((ValueError, ProviderError)):
        adapter.send(HANDLE, title, envelope())
    assert not factory.clients


def test_malformed_or_oversized_payload_is_rejected_without_contacting_smtp():
    factory = FakeSMTPFactory()
    adapter = SMTP163Transport(smtp_factory=factory)
    adapter.register_credentials(HANDLE, SENDER, AUTH_CODE)
    for content in (
        "not-json",
        envelope(body="x" * (128 * 1024)),
        envelope(recipient="x@example.invalid\nBcc:y@example.invalid"),
    ):
        with pytest.raises((ValueError, ProviderError)):
            adapter.send(HANDLE, "G15", content)
    assert not factory.clients


@pytest.mark.parametrize(
    "client_options,uncertain",
    [
        (
            {"login_error": smtplib.SMTPAuthenticationError(535, AUTH_CODE.encode())},
            False,
        ),
        ({"send_error": smtplib.SMTPServerDisconnected(AUTH_CODE)}, True),
        ({"refusals": {RECIPIENT: (550, AUTH_CODE.encode())}}, False),
    ],
)
def test_provider_errors_are_safe_and_data_disconnect_is_uncertain(
    client_options, uncertain
):
    factory = FakeSMTPFactory(**client_options)
    adapter = SMTP163Transport(smtp_factory=factory)
    adapter.register_credentials(HANDLE, SENDER, AUTH_CODE)
    with pytest.raises(ProviderError) as caught:
        adapter.send(HANDLE, "G15", envelope())
    assert caught.value.uncertain is uncertain
    assert AUTH_CODE not in str(caught.value)
    assert RECIPIENT not in str(caught.value)
    assert factory.clients[0].closed


def test_late_factory_cannot_login_or_send_after_deadline_and_workers_do_not_overlap():
    entered, release = threading.Event(), threading.Event()
    clients = []

    def late_factory(**options):
        entered.set()
        release.wait(2)
        client = FakeSMTP(options)
        clients.append(client)
        return client

    adapter = SMTP163Transport(timeout_seconds=0.03, smtp_factory=late_factory)
    adapter.register_credentials(HANDLE, SENDER, AUTH_CODE)
    started = time.monotonic()
    try:
        with pytest.raises(ProviderError):
            adapter.send(HANDLE, "G15", envelope())
        assert time.monotonic() - started < 0.5
        assert entered.is_set() and adapter.request_inflight
        with pytest.raises(ProviderError):
            adapter.send(HANDLE, "G15", envelope())
    finally:
        release.set()
        adapter._inflight.join(2)
    assert len(clients) == 1
    assert not clients[0].logins and not clients[0].sent and clients[0].closed
    assert not adapter.request_inflight


def test_production_smtp_constructor_uses_fixed_origin_and_verified_tls(monkeypatch):
    calls = []

    def no_network_init(_self, host, port, *, timeout, context):
        calls.append((host, port, timeout, context))

    monkeypatch.setattr(smtplib.SMTP_SSL, "__init__", no_network_init)
    smtp._DeadlineSMTP(
        deadline=time.monotonic() + 1,
        cancelled=threading.Event(),
        register_socket=lambda _sock: None,
        data_started=threading.Event(),
    )
    host, port, timeout, context = calls[0]
    assert (host, port) == ("smtp.163.com", 465)
    assert 0 < timeout <= 1
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_production_late_dns_result_cannot_open_a_socket(monkeypatch):
    client = object.__new__(smtp._DeadlineSMTP)
    client._deadline = time.monotonic() + 1
    client._cancelled = threading.Event()

    def late_dns(*_args):
        client._deadline = time.monotonic() - 1
        return [(2, 1, 6, "", ("127.0.0.1", 465))]

    def forbidden_socket(*_args):
        pytest.fail("late DNS must not open a socket")

    monkeypatch.setattr(smtp.socket, "getaddrinfo", late_dns)
    monkeypatch.setattr(smtp.socket, "socket", forbidden_socket)
    with pytest.raises(TimeoutError):
        client._get_socket("smtp.163.com", 465, 1)


def test_explicit_acceptance_survives_close_failure_without_retry():
    class BrokenClose(FakeSMTP):
        def close(self):
            raise OSError(AUTH_CODE)

    adapter = SMTP163Transport(smtp_factory=lambda **options: BrokenClose(options))
    adapter.register_credentials(HANDLE, SENDER, AUTH_CODE)
    assert adapter.send(HANDLE, "G15", envelope()).reference is None


def test_explicit_acceptance_survives_slow_close_but_workers_stay_bounded():
    closing, release = threading.Event(), threading.Event()

    class SlowClose(FakeSMTP):
        def close(self):
            closing.set()
            release.wait(2)

    adapter = SMTP163Transport(
        timeout_seconds=0.05, smtp_factory=lambda **options: SlowClose(options)
    )
    adapter.register_credentials(HANDLE, SENDER, AUTH_CODE)
    try:
        assert adapter.send(HANDLE, "G15", envelope()).reference is None
        assert closing.is_set() and adapter.request_inflight
        with pytest.raises(ProviderError, match="provider_request_in_progress"):
            adapter.send(HANDLE, "G15", envelope())
    finally:
        release.set()
        adapter._inflight.join(2)
    assert not adapter.request_inflight


def test_cooldown_retains_every_frozen_email_instead_of_coalescing(tmp_path):
    clock = Clock()
    service, factory = engine(
        tmp_path,
        clock=clock,
        policy=NotificationPolicy(enabled=True, cooldown_seconds=600),
    )
    try:
        identifiers = []
        for count in (1, 2, 3):
            decision = service.observe(snapshot(count))
            assert decision["decision"] == "queued"
            identifiers.append(decision["id"])
            clock.value += 5
        assert len(set(identifiers)) == 3
        assert service.status()["counts"] == {"queued": 3}
        assert service.observe(snapshot(3))["decision"] == "already_queued"
        clock.value = 3000
        for _ in range(3):
            assert service.dispatch_once()["decision"] == "provider_accepted"
        assert len(factory.sent) == 3
        bodies = [
            BytesParser(policy=email_policy.default)
            .parsebytes(wire)
            .get_body(preferencelist=("plain",))
            .get_content()
            for _, _, wire in factory.sent
        ]
        for count, body in zip((1, 2, 3), bodies):
            assert f"本运行累计红色告警事件：{count}" in body
    finally:
        service.close()


def test_restart_requires_reauthorization_but_preserves_frozen_recipient_and_reports(
    tmp_path,
):
    clock = Clock()
    first, first_factory = engine(tmp_path, clock=clock)
    report = snapshot()
    assert first.observe(report)["decision"] == "queued"
    first.configure_email(first.policy, sender=SENDER, recipient=OTHER_RECIPIENT)
    clock.value += 1
    assert first.observe(snapshot(2))["decision"] == "queued"
    first.close()
    factory = FakeSMTPFactory()
    restored = EmailNotificationService(
        tmp_path / "email-notifications.sqlite",
        transport=SMTP163Transport(smtp_factory=factory),
        clock=clock,
    )
    try:
        assert not restored.status()["configured"]
        assert restored.status()["counts"] == {"queued": 2}
        assert restored.dispatch_once()["decision"] == "token_missing"
        assert not factory.clients and not first_factory.clients
        restored.configure_email(
            restored.policy,
            sender=SENDER,
            recipient=OTHER_RECIPIENT,
            auth_code=OTHER_AUTH_CODE,
        )
        for _ in range(2):
            assert restored.dispatch_once()["decision"] == "provider_accepted"
        assert [recipients for _, recipients, _ in factory.sent] == [
            [RECIPIENT],
            [OTHER_RECIPIENT],
        ]
        original = BytesParser(policy=email_policy.default).parsebytes(
            factory.sent[0][2]
        )
        attachments = {
            part.get_filename(): part.get_payload(decode=True)
            for part in original.iter_attachments()
        }
        assert attachments["summary.md"] == report.report_markdown.encode("utf-8")
        assert json.loads(attachments["summary.json"]) == dict(report.report_json)
        for path in tmp_path.glob("email-notifications.sqlite*"):
            assert AUTH_CODE.encode() not in path.read_bytes()
            assert OTHER_AUTH_CODE.encode() not in path.read_bytes()
    finally:
        restored.close()


def test_sender_change_never_sends_old_queue_as_the_new_sender(tmp_path):
    clock = Clock()
    service, factory = engine(tmp_path, clock=clock)
    try:
        old = service.observe(snapshot())
        service.configure_email(
            service.policy,
            sender=OTHER_SENDER,
            recipient=OTHER_RECIPIENT,
            auth_code=OTHER_AUTH_CODE,
        )
        clock.value += 1
        assert service.observe(snapshot(2))["decision"] == "queued"
        result = service.dispatch_once()
        assert result["id"] == old["id"] and result["decision"] == "failed"
        assert not factory.clients
        assert service.dispatch_once()["decision"] == "provider_accepted"
        assert [(sender, recipients) for sender, recipients, _ in factory.sent] == [
            (OTHER_SENDER, [OTHER_RECIPIENT])
        ]
        assert factory.clients[0].logins == [(OTHER_SENDER, OTHER_AUTH_CODE)]
    finally:
        service.close()


def test_same_sender_auth_rotation_and_explicit_clear_are_memory_only(tmp_path):
    service, factory = engine(tmp_path)
    try:
        service.configure_email(service.policy, sender=SENDER, recipient=RECIPIENT)
        assert service.status()["configured"]
        assert service.enqueue_test(snapshot())["decision"] == "queued"
        service.configure_email(
            service.policy,
            sender=SENDER,
            recipient=RECIPIENT,
            auth_code=OTHER_AUTH_CODE,
        )
        assert service.dispatch_once()["decision"] == "provider_accepted"
        assert factory.clients[0].logins == [(SENDER, OTHER_AUTH_CODE)]
        service.configure_email(
            replace(service.policy, enabled=False),
            sender=SENDER,
            recipient=RECIPIENT,
            auth_code="",
        )
        assert not service.status()["configured"]
        assert service.enqueue_test(snapshot())["decision"] == "token_missing"
        safe = json.dumps(service.status(), ensure_ascii=False)
        assert AUTH_CODE not in safe and OTHER_AUTH_CODE not in safe
        with sqlite3.connect(tmp_path / "email-notifications.sqlite") as connection:
            persisted = "\n".join(connection.iterdump())
        assert AUTH_CODE not in persisted and OTHER_AUTH_CODE not in persisted
    finally:
        service.close()


def test_uncertain_retry_survives_restart_with_the_same_message_id_and_attachments(
    tmp_path,
):
    clock = Clock()
    first_factory = FakeSMTPFactory(
        send_error=smtplib.SMTPServerDisconnected("synthetic lost acknowledgement")
    )
    first, _ = engine(tmp_path, clock=clock, factory=first_factory)
    decision = first.enqueue_test(snapshot())
    failed_attempt = first.dispatch_once()
    assert failed_attempt["decision"] == "retry_wait" and failed_attempt["uncertain"]
    original = BytesParser(policy=email_policy.default).parsebytes(
        first_factory.sent[0][2]
    )
    assert original["Message-ID"]
    original_attachments = {
        part.get_filename(): part.get_payload(decode=True)
        for part in original.iter_attachments()
    }
    first.close()
    factory = FakeSMTPFactory()
    restored = EmailNotificationService(
        tmp_path / "email-notifications.sqlite",
        transport=SMTP163Transport(smtp_factory=factory),
        clock=clock,
    )
    try:
        restored.configure_email(
            restored.policy,
            sender=SENDER,
            recipient=OTHER_RECIPIENT,
            auth_code=OTHER_AUTH_CODE,
        )
        clock.value += 30
        assert restored.dispatch_once()["decision"] == "provider_accepted"
        assert restored.status()["latest"][0]["id"] == decision["id"]
        assert factory.sent[0][1] == [RECIPIENT]
        retried = BytesParser(policy=email_policy.default).parsebytes(
            factory.sent[0][2]
        )
        assert retried["Message-ID"] == original["Message-ID"]
        assert {
            part.get_filename(): part.get_payload(decode=True)
            for part in retried.iter_attachments()
        } == original_attachments
        assert (
            retried.get_body(preferencelist=("plain",)).get_content()
            == original.get_body(preferencelist=("plain",)).get_content()
        )
    finally:
        restored.close()


def test_same_sender_credential_rotation_between_claim_and_send_does_not_drop_mail(
    tmp_path, monkeypatch
):
    service, factory = engine(tmp_path)
    adapter = service._transport
    original_send = adapter.send

    def rotate_then_send(handle, title, content):
        service.configure_email(
            service.policy,
            sender=SENDER,
            recipient=OTHER_RECIPIENT,
            auth_code=OTHER_AUTH_CODE,
        )
        return original_send(handle, title, content)

    monkeypatch.setattr(adapter, "send", rotate_then_send)
    try:
        assert service.enqueue_test(snapshot())["decision"] == "queued"
        assert service.dispatch_once()["decision"] == "provider_accepted"
        assert factory.sent[0][1] == [RECIPIENT]
        assert factory.clients[0].logins == [(SENDER, OTHER_AUTH_CODE)]
    finally:
        service.close()
