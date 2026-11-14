"""Real HTTP boundaries for workbench reads and append-only disposition writes."""

import http.client
import json
import threading
from urllib.parse import urlencode

import pytest

from g15.console import server as console
from g15.console.operations import OperationsRequestError


class _Operations:
    def __init__(self):
        self.calls = []

    def overview(self):
        return {"available": True, "runs": []}

    def detail(self, identity):
        self.calls.append(("detail", identity))
        if identity == "missing":
            raise OperationsRequestError(404, "not_found")
        return {"id": identity}

    def compare(self, identities):
        self.calls.append(("compare", identities))
        return {"runs": identities}

    def dispositions(self, identity):
        self.calls.append(("dispositions", identity))
        return {"events": []}

    def record_disposition(self, payload):
        self.calls.append(("write", payload))
        return {"recorded": True}


@pytest.fixture
def service(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('[{"run_id":"RUN-1","device_id":"DEV-1"}]')
    operations = _Operations()
    server = console.create_console_server(
        catalog,
        tmp_path,
        "http://127.0.0.1:1",
        None,
        credential_provider=lambda: "server-only-token",
        operations_provider=operations,
    )
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield server, operations
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _request(service, method, route, payload=None, *, headers=None):
    server, _operations = service
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request(method, route, body=payload, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.mark.parametrize(
    "route",
    [
        "/api/workbench",
        "/api/workbench/run?id=live%3ARUN-1",
        "/api/workbench/compare?ids=live%3ARUN-1,experiment%3Alab-test",
        "/api/workbench/dispositions?id=live%3ARUN-1",
    ],
)
def test_allowlisted_workbench_get_routes_have_security_headers(service, route):
    status, headers, data = _request(service, "GET", route)
    assert status == 200 and isinstance(json.loads(data), dict)
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert b"server-only-token" not in data


@pytest.mark.parametrize(
    "route",
    [
        "/api/workbench?unexpected=yes",
        "/api/workbench/run",
        "/api/workbench/run?id=x&id=y",
        "/api/workbench/run?id=x&path=outside",
        "/api/workbench/compare?ids=a,a",
        "/api/workbench/compare?ids=a,b,c,d,e",
        "/api/workbench/compare?ids=a&extra=x",
        "/api/workbench/dispositions?id=x&extra=x",
    ],
)
def test_invalid_selectors_are_rejected_before_controller(service, route):
    assert _request(service, "GET", route)[0] == 400
    assert service[1].calls == []


def test_unavailable_and_not_found_remain_distinct(service):
    assert _request(service, "GET", "/api/workbench/run?id=missing")[0] == 404
    service[0].application.operations = None
    assert _request(service, "GET", "/api/workbench")[0] == 503


def test_post_requires_unique_same_origin_and_action_marker(service):
    port = service[0].server_port
    headers = {
        "Origin": f"http://127.0.0.1:{port}",
        "Content-Type": "application/json",
        "X-G15-Console-Action": "workbench",
    }
    payload = '{"id":"live:RUN-1","action":"acknowledge"}'
    assert (
        _request(
            service, "POST", "/api/workbench/dispositions", payload, headers=headers
        )[0]
        == 200
    )
    assert len(service[1].calls) == 1
    for changed in (
        {**headers, "Origin": "http://example.com"},
        {key: value for key, value in headers.items() if key != "Origin"},
        {**headers, "X-G15-Console-Action": "laboratory"},
    ):
        assert (
            _request(
                service, "POST", "/api/workbench/dispositions", payload, headers=changed
            )[0]
            == 403
        )
    assert len(service[1].calls) == 1
    for bad in ('{"id":"x","id":"y"}', '{"value":NaN}', '"string"', " " * 16385):
        assert (
            _request(
                service, "POST", "/api/workbench/dispositions", bad, headers=headers
            )[0]
            == 400
        )
    assert len(service[1].calls) == 1


def test_current_resource_authorization_checks_catalog_then_upstream(
    service, monkeypatch
):
    application = service[0].application
    calls = []
    monkeypatch.setattr(
        application,
        "upstream_get",
        lambda pair, params: (calls.append(pair) or (401, b"{}")),
    )
    with pytest.raises(console.ConsoleError) as absent:
        application.authorize_operation(("outside", "DEV-1"))
    assert absent.value.status == 404 and calls == []
    with pytest.raises(console.ConsoleError) as denied:
        application.authorize_operation(("RUN-1", "DEV-1"))
    assert denied.value.status == 401 and calls == [("RUN-1", "DEV-1")]


def test_post_reauthorization_denial_keeps_authentication_challenge(
    service, monkeypatch
):
    def denied(_payload):
        raise console.ConsoleError(401, "workbench_access_denied")

    monkeypatch.setattr(service[1], "record_disposition", denied)
    status, headers, payload = _request(
        service,
        "POST",
        "/api/workbench/dispositions",
        "{}",
        headers={
            "Origin": f"http://127.0.0.1:{service[0].server_port}",
            "Content-Type": "application/json",
            "X-G15-Console-Action": "workbench",
        },
    )
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    assert json.loads(payload)["error"] == "workbench_access_denied"


def test_waveform_uses_registered_location_and_shared_capacity(
    service, monkeypatch, tmp_path
):
    application = service[0].application
    monkeypatch.setattr(
        service[1],
        "resolve_waveform",
        lambda _id: (tmp_path, "RUN-1", "DEV-1"),
        raising=False,
    )
    observed = []
    monkeypatch.setattr(
        console,
        "read_waveform",
        lambda *args, **kw: (observed.append((args, kw)) or {"samples": []}),
    )
    route = "/api/workbench/waveform?" + urlencode(
        {"id": "live:RUN-1", "start_seq": 12, "count": 256}
    )
    assert _request(service, "GET", route)[0] == 200
    assert observed == [((tmp_path, "RUN-1", "DEV-1"), {"start_seq": 12, "count": 256})]
    assert application._waveform_slots.acquire(blocking=False)
    assert application._waveform_slots.acquire(blocking=False)
    try:
        assert _request(service, "GET", route)[0] == 503
        assert len(observed) == 1
    finally:
        application._waveform_slots.release()
        application._waveform_slots.release()
