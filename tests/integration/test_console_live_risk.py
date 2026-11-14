"""Real HTTP origin, authorization and projection boundaries for live risk control."""

from copy import deepcopy
import http.client
import json
import threading

import pytest

from g15.console import server as console
from g15.live.risk import LiveRiskError


def _snapshot():
    event = {
        "event_id": "risk-0123456789abcdef",
        "fault_type": "step",
        "amplitude": 3.0,
        "delay_seconds": 1.0,
        "duration_seconds": 2.0,
        "start_seq": 256,
        "end_seq_exclusive": 768,
        "duration_points": 512,
        "created_at_utc": "2026-11-14T00:00:00Z",
        "status": "active",
        "captured_points": 256,
        "processed_points": 0,
        "cancel_seq": None,
        "cancelled_at_utc": None,
        "cancel_reason": None,
        "private_truth": "private-runtime-path",
    }
    pair = {"run_id": "RUN-1", "device_id": "DEV-1"}
    return {
        "schema_version": "1",
        "session_id": "live-risk-test",
        "status": "running",
        "version": 1,
        "controls_available": True,
        "limits": {
            "fault_types": ["spike", "step", "burst"],
            "max_amplitude": 10,
            "max_delay_seconds": 300,
            "max_duration_seconds": 300,
            "min_duration_points": {"spike": 1, "step": 1, "burst": 3},
            "max_history": 256,
            "max_requests": 1024,
        },
        "devices": [
            {**pair, "mode": "normal", "capture_paused": False, "event": event}
        ],
        "history": [{**pair, **event}],
        "history_count": 1,
        "token": "server-only-token",
    }


class _Risk:
    def __init__(self):
        self.state = _snapshot()
        self.calls = []
        self.failure = None
        self.replayed = False

    def snapshot(self):
        return deepcopy(self.state)

    def control(self, payload):
        self.calls.append(deepcopy(payload))
        if self.failure is not None:
            raise self.failure
        result = self.snapshot()
        result["request"] = {
            "request_id": payload["request_id"],
            "action": payload["action"],
            "event_id": self.state["devices"][0]["event"]["event_id"],
            "replayed": self.replayed,
            "path": "private-runtime-path",
        }
        return result


@pytest.fixture
def service(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('[{"run_id":"RUN-1","device_id":"DEV-1"}]', encoding="utf-8")
    risk = _Risk()
    server = console.create_console_server(
        catalog,
        tmp_path,
        "http://127.0.0.1:1",
        None,
        credential_provider=lambda: "server-only-token",
        live_risk_provider=risk.snapshot,
        live_risk_control_provider=risk.control,
    )
    authorizations = []
    monkeypatch.setattr(
        server.application,
        "upstream_get",
        lambda pair, params: (authorizations.append((pair, params)) or (200, b"{}")),
    )
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield server, risk, authorizations
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _headers(service):
    return {
        "Origin": f"http://127.0.0.1:{service[0].server_port}",
        "Content-Type": "application/json",
        "X-G15-Console-Action": "live-risk",
    }


def _payload(action="inject"):
    result = {
        "session_id": "live-risk-test",
        "run_id": "RUN-1",
        "device_id": "DEV-1",
        "request_id": "request-1",
        "action": action,
    }
    if action == "inject":
        result.update(
            fault_type="step", amplitude=3, delay_seconds=1, duration_seconds=2
        )
    return result


def _request(
    service, method="GET", *, route="/api/live/risk", payload=None, headers=None
):
    connection = http.client.HTTPConnection(*service[0].server_address, timeout=3)
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    try:
        connection.request(method, route, body=payload, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_get_and_head_project_only_registered_public_risk_state(service):
    status, headers, data = _request(service)
    assert status == 200
    body = json.loads(data)
    assert body["devices"][0]["event"]["captured_points"] == 256
    assert body["history_count"] == 1 and len(body["history"]) == 1
    assert body["controls_available"] is True
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert b"private-runtime-path" not in data and b"server-only-token" not in data
    assert b"private_truth" not in data
    head_status, head_headers, head_data = _request(service, "HEAD")
    assert head_status == 200 and head_data == b""
    assert int(head_headers["Content-Length"]) == len(data)
    assert service[1].calls == [] and service[2] == []


def test_live_risk_module_is_served_to_the_simulation_page(service):
    status, headers, data = _request(service, route="/assets/live-risk.js")
    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert b"export " in data and b"/api/live/risk" in data
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_risk_provider_and_controls_are_opt_in(service):
    service[0].application.live_risk_control_provider = None
    assert json.loads(_request(service)[2])["controls_available"] is False
    assert (
        _request(service, "POST", payload=_payload(), headers=_headers(service))[0]
        == 503
    )
    service[0].application.live_risk_provider = None
    assert _request(service)[0] == 404
    assert service[2] == []


@pytest.mark.parametrize(
    "route",
    [
        "/api/live/risk?device_id=DEV-1",
        "/api/live/risk?run_id=RUN-1&run_id=RUN-1",
        "/api/live/risk?unknown=1",
    ],
)
def test_get_rejects_selectors(service, route):
    assert _request(service, route=route)[0] == 400
    assert service[2] == []


@pytest.mark.parametrize(
    "location,value",
    [
        (("devices",), []),
        (("devices", 0, "run_id"), "outside"),
        (("devices", 0, "capture_paused"), 1),
        (("devices", 0, "mode"), "private-runtime-path"),
        (("devices", 0, "event", "amplitude"), float("inf")),
        (("devices", 0, "event", "processed_points"), 257),
        (("devices", 0, "event", "cancel_reason"), "private-runtime-path"),
        (("history", 0, "device_id"), "other-device"),
        (("history_count",), 257),
        (("version",), True),
        (("session_id",), "../../private-runtime-path"),
    ],
)
def test_invalid_provider_state_fails_closed_without_private_details(
    service, location, value
):
    target = service[1].state
    for key in location[:-1]:
        target = target[key]
    target[location[-1]] = value
    status, _, body = _request(service)
    assert status == 503 and json.loads(body) == {"error": "live_risk_unavailable"}


@pytest.mark.parametrize("action", ["inject", "clear", "automatic"])
def test_post_requires_registered_pair_and_current_upstream_authorization(
    service, action
):
    payload = _payload(action)
    status, _, body = _request(
        service, "POST", payload=payload, headers=_headers(service)
    )
    assert status == 200
    assert service[1].calls == [payload]
    assert service[2] == [(("RUN-1", "DEV-1"), {"page_size": "1"})]
    result = json.loads(body)
    assert result["request"] == {
        "request_id": "request-1",
        "action": action,
        "event_id": "risk-0123456789abcdef",
        "replayed": False,
    }
    assert b"private-runtime-path" not in body and b"server-only-token" not in body


def test_replay_is_reauthorized_and_runtime_may_replay_after_stop(service):
    service[1].state["status"] = "stopped"
    service[1].state["controls_available"] = False
    service[1].replayed = True
    for _ in range(2):
        status, _, data = _request(
            service, "POST", payload=_payload(), headers=_headers(service)
        )
        assert status == 200 and json.loads(data)["request"]["replayed"] is True
    assert len(service[2]) == 2 and len(service[1].calls) == 2


@pytest.mark.parametrize(
    "removed", ["session_id", "run_id", "device_id", "request_id", "action"]
)
def test_post_rejects_missing_identity_before_authorization(service, removed):
    payload = _payload()
    del payload[removed]
    assert (
        _request(service, "POST", payload=payload, headers=_headers(service))[0] == 400
    )
    assert service[1].calls == [] and service[2] == []


@pytest.mark.parametrize(
    "changed",
    [
        {"session_id": ""},
        {"run_id": []},
        {"request_id": "../bad"},
        {"action": "pause"},
        {"unknown": True},
        {"action": "clear"},
    ],
)
def test_post_rejects_noncanonical_request_shape(service, changed):
    payload = {**_payload(), **changed}
    assert (
        _request(service, "POST", payload=payload, headers=_headers(service))[0] == 400
    )
    assert service[1].calls == [] and service[2] == []


def test_unknown_pair_and_upstream_denial_never_reach_runtime(service, monkeypatch):
    payload = {**_payload(), "run_id": "unknown"}
    assert (
        _request(service, "POST", payload=payload, headers=_headers(service))[0] == 404
    )
    assert service[2] == []
    monkeypatch.setattr(
        service[0].application,
        "upstream_get",
        lambda *_: (401, b"private-runtime-path"),
    )
    status, headers, body = _request(
        service, "POST", payload=_payload(), headers=_headers(service)
    )
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    assert json.loads(body) == {"error": "live_risk_access_denied"}
    assert service[1].calls == []


@pytest.mark.parametrize(
    "changed",
    [
        {"Origin": "http://example.com"},
        {"Origin": None},
        {"X-G15-Console-Action": "live-control"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_post_rejects_cross_origin_or_missing_marker_before_authorization(
    service, changed
):
    headers = {
        key: value
        for key, value in {**_headers(service), **changed}.items()
        if value is not None
    }
    assert _request(service, "POST", payload=_payload(), headers=headers)[0] == 403
    assert service[1].calls == [] and service[2] == []


@pytest.mark.parametrize(
    "body",
    [
        '{"session_id":"x","session_id":"y"}',
        '{"amplitude":NaN}',
        '{"amplitude":Infinity}',
        '"string"',
        " " * 4097,
    ],
)
def test_post_rejects_duplicate_json_keys_nonfinite_values_and_oversize(service, body):
    assert _request(service, "POST", payload=body, headers=_headers(service))[0] == 400
    assert service[1].calls == [] and service[2] == []


@pytest.mark.parametrize(
    "header", ["Origin", "X-G15-Console-Action", "Content-Length", "Content-Type"]
)
def test_post_rejects_duplicate_security_headers(service, header):
    connection = http.client.HTTPConnection(*service[0].server_address, timeout=3)
    body = json.dumps(_payload()).encode()
    headers = {**_headers(service), "Content-Length": str(len(body))}
    try:
        connection.putrequest("POST", "/api/live/risk")
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.putheader(header, headers[header])
        connection.endheaders(body)
        response = connection.getresponse()
        assert response.status == (
            403 if header in ("Origin", "X-G15-Console-Action") else 400
        )
        response.read()
    finally:
        connection.close()
    assert service[1].calls == [] and service[2] == []


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "invalid_live_risk_request"),
        (404, "live_risk_device_not_found"),
        (409, "live_risk_session_changed"),
        (409, "live_risk_not_running"),
        (409, "live_risk_busy"),
        (409, "live_risk_request_conflict"),
        (409, "live_risk_history_full"),
        (503, "live_risk_persistence_failed"),
    ],
)
def test_runtime_fixed_errors_are_preserved_without_exception_text(
    service, status, code
):
    service[1].failure = LiveRiskError(status, code, "private-runtime-path")
    actual, _, body = _request(
        service, "POST", payload=_payload(), headers=_headers(service)
    )
    assert actual == status and json.loads(body) == {"error": code}


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("private-runtime-path"),
        LiveRiskError(503, "private-runtime-path", "private-runtime-path"),
    ],
)
def test_unexpected_runtime_failures_are_sanitized(service, error):
    service[1].failure = error
    status, _, body = _request(
        service, "POST", payload=_payload(), headers=_headers(service)
    )
    assert status == 503 and json.loads(body) == {"error": "live_risk_unavailable"}


def test_invalid_control_receipt_is_not_returned(service, monkeypatch):
    original = service[1].control

    def mismatched(payload):
        response = original(payload)
        response["request"]["request_id"] = "other-request"
        return response

    monkeypatch.setattr(
        service[0].application, "live_risk_control_provider", mismatched
    )
    status, _, body = _request(
        service, "POST", payload=_payload(), headers=_headers(service)
    )
    assert status == 503 and json.loads(body) == {"error": "live_risk_unavailable"}
