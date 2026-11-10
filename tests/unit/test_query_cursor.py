"""Unit tests for keyset pagination cursors (ADR 5.1.5).

A cursor is HMAC-integrity-protected and binds its scope: query kind, run,
device, actor, policy version and page size. A tampered or mismatched cursor
must be rejected as an invalid parameter before any database access.
"""

from __future__ import annotations

import pytest

from g15.query.cursor import (
    QUERY_ALARMS,
    QUERY_WINDOWS,
    CursorState,
    decode_cursor,
    encode_cursor,
)
from g15.query.errors import InvalidParameterError

KEY = b"k" * 32


def _windows_state(**overrides):
    base = {
        "actor_ref": "s1",
        "policy_version": "v1",
        "query": QUERY_WINDOWS,
        "run_id": "r1",
        "device_id": "d1",
        "page_size": 10,
        "window_high_water": 100,
        "last_start_seq": 90,
    }
    base.update(overrides)
    return CursorState(**base)


def _alarms_state(**overrides):
    base = {
        "actor_ref": "s1",
        "policy_version": "v1",
        "query": QUERY_ALARMS,
        "run_id": "r1",
        "device_id": "d1",
        "page_size": 10,
        "alarm_high_water_start_seq": 100,
        "alarm_high_water_rule_id": "rule_beta",
        "last_start_seq": 90,
        "last_rule_id": "rule_alpha",
    }
    base.update(overrides)
    return CursorState(**base)


def test_windows_round_trip():
    state = _windows_state()
    decoded = decode_cursor(encode_cursor(state, KEY), KEY)
    assert decoded.query == QUERY_WINDOWS
    assert decoded.run_id == "r1"
    assert decoded.device_id == "d1"
    assert decoded.last_start_seq == 90
    assert decoded.window_high_water == 100


def test_alarms_round_trip():
    state = _alarms_state()
    decoded = decode_cursor(encode_cursor(state, KEY), KEY)
    assert decoded.query == QUERY_ALARMS
    assert decoded.last_rule_id == "rule_alpha"
    assert decoded.alarm_high_water_rule_id == "rule_beta"


def test_wrong_key_rejected():
    token = encode_cursor(_windows_state(), KEY)
    with pytest.raises(InvalidParameterError):
        decode_cursor(token, b"z" * 32)


def test_tampered_payload_rejected():
    token = encode_cursor(_windows_state(), KEY)
    prefix, body, sig = token.split(".")
    body = "A" + body[1:]
    with pytest.raises(InvalidParameterError):
        decode_cursor(prefix + "." + body + "." + sig, KEY)


def test_missing_segment_rejected():
    with pytest.raises(InvalidParameterError):
        decode_cursor("g15c1.onlytwo", KEY)


def test_wrong_prefix_rejected():
    token = encode_cursor(_windows_state(), KEY)
    _, body, sig = token.split(".")
    with pytest.raises(InvalidParameterError):
        decode_cursor("evil." + body + "." + sig, KEY)


def test_unknown_query_kind_rejected():
    state = _windows_state(query="other")
    token = encode_cursor(state, KEY)
    with pytest.raises(InvalidParameterError):
        decode_cursor(token, KEY)


def test_windows_without_high_water_rejected():
    state = _windows_state()
    state = CursorState(
        actor_ref="s1",
        policy_version="v1",
        query=QUERY_WINDOWS,
        run_id="r1",
        device_id="d1",
        page_size=10,
        last_start_seq=90,
    )
    token = encode_cursor(state, KEY)
    with pytest.raises(InvalidParameterError):
        decode_cursor(token, KEY)


def test_alarms_without_high_water_rejected():
    state = CursorState(
        actor_ref="s1",
        policy_version="v1",
        query=QUERY_ALARMS,
        run_id="r1",
        device_id="d1",
        page_size=10,
        last_start_seq=90,
        last_rule_id="rule_alpha",
    )
    token = encode_cursor(state, KEY)
    with pytest.raises(InvalidParameterError):
        decode_cursor(token, KEY)


def test_unknown_payload_key_rejected():
    import base64
    import hashlib
    import hmac
    import json

    payload = {
        "v": 1,
        "actor_ref": "s1",
        "policy_version": "v1",
        "query": QUERY_WINDOWS,
        "run_id": "r1",
        "device_id": "d1",
        "page_size": 10,
        "window_high_water": 100,
        "last_start_seq": 90,
        "extra": "x",
    }
    body = (
        base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode())
        .rstrip(b"=")
        .decode()
    )
    mac = hmac.new(KEY, ("cursor-v1." + body).encode(), hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(mac).rstrip(b"=").decode()
    with pytest.raises(InvalidParameterError):
        decode_cursor("g15c1." + body + "." + sig, KEY)
