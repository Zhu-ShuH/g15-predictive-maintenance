"""PushPlus HTTP contract tests; every network connection is a local mock."""

import json
import ssl
from types import SimpleNamespace

import pytest

from g15.notifications import ProviderError, ProviderReceipt, PushPlusTransport
from g15.notifications import transport

TOKEN = "fake-provider-token-123456"


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.fp = None

    def read1(self, size):
        value, self.body = self.body[:size], self.body[size:]
        return value


def fake_http(monkeypatch, *, body=None, status=200, failure=None):
    calls = []

    class Connection:
        def __init__(self, host, *, timeout, context):
            assert host == "www.pushplus.plus"
            assert 0 < timeout <= 5
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname is True
            self.sock = SimpleNamespace(settimeout=lambda _value: None)
            calls.append(self)
            self.request_args = None
            self.closed = False

        def connect(self):
            pass

        def request(self, method, path, *, body, headers):
            self.request_args = method, path, json.loads(body), headers
            if failure:
                raise failure

        def getresponse(self):
            return Response(
                body if body is not None else b'{"code":200,"data":"opaque-code"}',
                status,
            )

        def close(self):
            self.closed = True

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    return calls


def test_https_json_post_uses_fixed_endpoint_full_content_and_acceptance_only(
    monkeypatch,
):
    calls = fake_http(monkeypatch)
    content = "# 完整报告\n\n" + "正文" * 100
    receipt = PushPlusTransport().send(TOKEN, "G15 红色告警", content)
    assert receipt == ProviderReceipt("opaque-code")
    method, path, body, headers = calls[0].request_args
    assert method == "POST" and path == "/send"
    assert body == {
        "token": TOKEN,
        "title": "G15 红色告警",
        "content": content,
        "template": "markdown",
        "channel": "wechat",
    }
    assert headers["Content-Type"].startswith("application/json")
    assert calls[0].closed


@pytest.mark.parametrize(
    "body,error,uncertain",
    [
        (b'{"code":400,"msg":"private token secret"}', "provider_rejected", False),
        (b'{"code":500}', "provider_rejected", False),
        (b'{"code":true}', "provider_invalid_response", True),
        (b'{"code":"200"}', "provider_invalid_response", True),
        (b"not json", "provider_invalid_response", True),
        (b"\xff", "provider_invalid_response", True),
        (b"[]", "provider_invalid_response", True),
        (b"x" * (64 * 1024 + 1), "provider_response_too_large", True),
    ],
    ids=[
        "rejected",
        "server-error",
        "bool-code",
        "text-code",
        "invalid-json",
        "invalid-utf8",
        "array",
        "oversized",
    ],
)
def test_invalid_rejected_or_oversized_provider_reply_is_not_success(
    monkeypatch, body, error, uncertain
):
    calls = fake_http(monkeypatch, body=body)
    with pytest.raises(ProviderError) as caught:
        PushPlusTransport().send(TOKEN, "title", "full body")
    assert str(caught.value) == error
    assert caught.value.uncertain is uncertain
    assert TOKEN not in str(caught.value)
    assert "private token" not in str(caught.value)
    assert calls[0].closed


@pytest.mark.parametrize(
    "status,retryable,error",
    [
        (301, False, "provider_redirect_rejected"),
        (307, False, "provider_redirect_rejected"),
        (400, False, "provider_http_error"),
        (401, False, "provider_http_error"),
        (429, True, "provider_http_error"),
        (500, True, "provider_http_error"),
    ],
)
def test_http_errors_and_redirects_are_bounded_without_following(
    monkeypatch, status, retryable, error
):
    calls = fake_http(monkeypatch, status=status)
    with pytest.raises(ProviderError) as caught:
        PushPlusTransport().send(TOKEN, "title", "body")
    assert str(caught.value) == error
    assert caught.value.retryable is retryable
    assert len(calls) == 1
    assert calls[0].closed


def test_timeout_has_fixed_code_and_no_exception_text_leak(monkeypatch):
    calls = fake_http(monkeypatch, failure=TimeoutError(TOKEN))
    with pytest.raises(ProviderError) as caught:
        PushPlusTransport().send(TOKEN, "title", "body")
    assert str(caught.value) == "provider_timeout"
    assert caught.value.uncertain is True
    assert TOKEN not in str(caught.value)
    assert calls[0].closed


@pytest.mark.parametrize(
    "reference",
    [
        TOKEN,
        {"shortcode": TOKEN},
        "x" * 129,
        {"shortCode": ["unsafe"]},
        "../../private-path",
    ],
)
def test_untrusted_provider_reference_cannot_leak_credentials_or_arbitrary_objects(
    monkeypatch, reference
):
    fake_http(monkeypatch, body=json.dumps({"code": 200, "data": reference}).encode())
    assert PushPlusTransport().send(TOKEN, "title", "body").reference is None


def test_request_size_limit_fails_without_network_or_truncating(monkeypatch):
    calls = fake_http(monkeypatch)
    with pytest.raises(ProviderError, match="provider_payload_too_large"):
        PushPlusTransport().send(TOKEN, "title", "x" * (128 * 1024))
    assert not calls


@pytest.mark.parametrize("value", [True, 0, -1, 6, float("inf"), float("nan")])
def test_transport_timeout_must_be_finite_and_at_most_five_seconds(value):
    with pytest.raises(ValueError):
        PushPlusTransport(value)


def test_stuck_dns_worker_is_bounded_and_cannot_spawn_overlapping_requests(monkeypatch):
    import threading
    import time

    entered, release = threading.Event(), threading.Event()
    request_calls = []

    def stuck_request(_self, _token, _body, _deadline):
        request_calls.append(1)
        entered.set()
        release.wait(2)
        raise ProviderError("provider_timeout", uncertain=True)

    monkeypatch.setattr(PushPlusTransport, "_send_request", stuck_request)
    adapter = PushPlusTransport(timeout_seconds=0.03)
    started = time.monotonic()
    try:
        with pytest.raises(ProviderError, match="provider_timeout"):
            adapter.send(TOKEN, "title", "full report")
        assert time.monotonic() - started < 0.5
        assert entered.is_set() and adapter.request_inflight
        with pytest.raises(ProviderError, match="provider_request_in_progress"):
            adapter.send(TOKEN, "title", "full report")
        assert len(request_calls) == 1
    finally:
        release.set()
        adapter._inflight.join(2)
    assert not adapter.request_inflight


def test_dns_finishing_after_deadline_never_posts_body(monkeypatch):
    import time

    calls = fake_http(monkeypatch)
    original = transport.http.client.HTTPSConnection.connect

    def delayed_connect(connection):
        time.sleep(0.04)
        return original(connection)

    monkeypatch.setattr(
        transport.http.client.HTTPSConnection, "connect", delayed_connect
    )
    adapter = PushPlusTransport(timeout_seconds=0.02)
    with pytest.raises(ProviderError, match="provider_timeout"):
        adapter.send(TOKEN, "title", "full report")
    adapter._inflight.join(1)
    assert calls[0].request_args is None
    assert calls[0].closed


def test_partial_read_past_deadline_remains_timeout_not_accepted(monkeypatch):
    import time

    calls = fake_http(monkeypatch)
    original = Response.read1

    def slow_read(response, size):
        time.sleep(0.03)
        return original(response, size)

    monkeypatch.setattr(Response, "read1", slow_read)
    adapter = PushPlusTransport(timeout_seconds=0.02)
    with pytest.raises(ProviderError, match="provider_timeout") as caught:
        adapter.send(TOKEN, "title", "full report")
    assert caught.value.uncertain is True
    adapter._inflight.join(1)
    assert calls[0].closed


def test_service_close_exposes_stuck_dns_request_worker(tmp_path, monkeypatch):
    import threading

    from g15.notifications import AlertSnapshot, NotificationPolicy, NotificationService

    entered, release = threading.Event(), threading.Event()

    def stuck_request(_self, _token, _body, _deadline):
        entered.set()
        release.wait(2)
        raise ProviderError("provider_timeout", uncertain=True)

    monkeypatch.setattr(PushPlusTransport, "_send_request", stuck_request)
    adapter = PushPlusTransport(timeout_seconds=0.02)
    engine = NotificationService(
        tmp_path / "outbox.sqlite", NotificationPolicy(), transport=adapter
    )
    engine.configure(engine.policy, TOKEN)
    row = AlertSnapshot(
        "run-1",
        "device-1",
        0,
        None,
        {"run_id": "run-1", "device_id": "device-1"},
        "# Full report",
        (),
    )
    engine.enqueue_test(row)
    try:
        assert engine.dispatch_once()["decision"] == "retry_wait"
        assert entered.is_set()
        with pytest.raises(RuntimeError, match="notification_request_unfinished"):
            engine.close()
        assert engine.status()["last_error"] == "notification_request_unfinished"
        assert engine.status()["latest"][0]["uncertain"] is True
    finally:
        release.set()
        adapter._inflight.join(2)
    engine.close()
