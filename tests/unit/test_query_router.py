"""Unit tests for the thin query transport (ADR 5.1.4 error matrix).

The router maps the QueryError family onto the status matrix and, on success,
returns an allow-listed body plus a trace whose stages are recorded in order.
The failure assertions cover: missing/malformed/forged bearer -> 401,
authorised-but-absent/forbidden resource -> 404, invalid parameter -> 400.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from g15.query.authz import load_authz
from g15.query.bearer import sign_token
from g15.service.router import QueryRequest, QueryRouter

KEY = b"k" * 32
ISSUER = "issuer"
AUDIENCE = "audience"

_AUTHZ = load_authz("""authz_schema_version: "1"
policy_version: "v1"
subjects:
  s1:
    actor_ref: "s1"
    permission_domain: "d"
    allow:
      - run_id: "run1"
        device_id: "dev1"
""")


def _token(sub="s1"):
    return "Bearer " + sign_token(
        {
            "typ": "g15-query-access",
            "ver": "1",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": sub,
            "jti": "j1",
            "iat": 1000,
            "exp": 1600,
        },
        KEY,
    )


def _router(runs=None, policy=_AUTHZ, now=None):
    runs = runs or {"run1": Path("/nonexistent")}
    return QueryRouter(
        key=KEY,
        policy=policy,
        runs=runs,
        issuer=ISSUER,
        audience=AUDIENCE,
        now=now or (lambda: 1500),
    )


def test_router_rejects_short_key():
    with pytest.raises(ValueError):
        QueryRouter(key=b"x", policy=_AUTHZ, runs={}, issuer=ISSUER, audience=AUDIENCE)


def test_router_rejects_empty_issuer():
    with pytest.raises(ValueError):
        QueryRouter(key=KEY, policy=_AUTHZ, runs={}, issuer="", audience=AUDIENCE)


def test_missing_authorization_is_401():
    r = _router().handle(
        QueryRequest(authorization=None, run_id="run1", device_id="dev1", kind="health")
    )
    assert r.status == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"
    assert r.body["error"] == "unauthorized"


def test_bad_bearer_is_401():
    r = _router().handle(
        QueryRequest(
            authorization="Bearer g15q1.bad.sig",
            run_id="run1",
            device_id="dev1",
            kind="health",
        )
    )
    assert r.status == 401
    assert r.body["error"] == "unauthorized"


def test_wrong_scheme_is_401():
    r = _router().handle(
        QueryRequest(
            authorization=_token().replace("Bearer", "Basic"),
            run_id="run1",
            device_id="dev1",
            kind="health",
        )
    )
    assert r.status == 401


def test_unknown_kind_is_400():
    r = _router().handle(
        QueryRequest(
            authorization=_token(), run_id="run1", device_id="dev1", kind="bogus"
        )
    )
    assert r.status == 400
    assert r.body["error"] == "invalid_request"


def test_illegal_run_id_is_400():
    r = _router().handle(
        QueryRequest(
            authorization=_token(), run_id="a/../b", device_id="dev1", kind="health"
        )
    )
    assert r.status == 400


def test_page_size_out_of_range_is_400():
    r = _router().handle(
        QueryRequest(
            authorization=_token(),
            run_id="run1",
            device_id="dev1",
            kind="windows",
            page_size=999,
        )
    )
    assert r.status == 400
    r = _router().handle(
        QueryRequest(
            authorization=_token(),
            run_id="run1",
            device_id="dev1",
            kind="windows",
            page_size=0,
        )
    )
    assert r.status == 400


def test_forbidden_resource_is_404():
    # s1 is authorised for run1/dev1; ask for a different device.
    r = _router().handle(
        QueryRequest(
            authorization=_token(), run_id="run1", device_id="dev2", kind="health"
        )
    )
    assert r.status == 404
    assert r.body["error"] == "not_found"


def test_unknown_run_is_404():
    r = _router().handle(
        QueryRequest(
            authorization=_token(), run_id="nope", device_id="dev1", kind="health"
        )
    )
    assert r.status == 404


def test_tampered_cursor_is_400():
    r = _router().handle(
        QueryRequest(
            authorization=_token(),
            run_id="run1",
            device_id="dev1",
            kind="windows",
            cursor="g15c1.abc.def",
        )
    )
    assert r.status == 400


def test_rejected_request_records_rejected_stage():
    r = _router().handle(
        QueryRequest(authorization=None, run_id="run1", device_id="dev1", kind="health")
    )
    stages = [e.stage for e in r.trace.events]
    assert stages[0] == "server_received"
    assert stages[-1] == "rejected"
