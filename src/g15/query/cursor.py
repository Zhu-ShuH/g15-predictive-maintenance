"""Stable keyset pagination cursors for the D0 query subset.

Implements ADR-0007 section 5.1.5: results are ordered deterministically and
paginated by keyset, never by a mutable offset. Each cursor binds its query
scope (run, device, sort, page size, high-water mark) and the last key of the
previous page, and is integrity-protected with HMAC so a tampered cursor is
rejected before any database access.

There are two independent cursor families, matching the two result kinds:

* windows  - feature_windows ordered by start_seq DESC
* alarms   - alarms ordered by start_seq DESC, rule_id DESC (rule_id breaks
             ties on start_seq)

A cursor is project-specific, like the bearer: g15c1.<payload-b64url>.<sig>.
The signature domain uses a distinct prefix so a cursor can never be replayed
as a token and vice versa.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from g15.query.errors import InvalidParameterError

CURSOR_PREFIX = "g15c1"
CURSOR_VERSION = 1
CURSOR_SIGNING_LABEL = "cursor-v1"
MAX_CURSOR_BYTES = 4096

QUERY_WINDOWS = "windows"
QUERY_ALARMS = "alarms"
_QUERY_KINDS = frozenset({QUERY_WINDOWS, QUERY_ALARMS})

_CURSOR_KEYS = frozenset(
    {
        "v",
        "actor_ref",
        "policy_version",
        "query",
        "run_id",
        "device_id",
        "page_size",
        "window_high_water",
        "alarm_high_water_start_seq",
        "alarm_high_water_rule_id",
        "last_start_seq",
        "last_rule_id",
    }
)


@dataclass(frozen=True)
class CursorState:
    actor_ref: str
    policy_version: str
    query: str
    run_id: str
    device_id: str
    page_size: int
    window_high_water: int | None = None
    alarm_high_water_start_seq: int | None = None
    alarm_high_water_rule_id: str | None = None
    last_start_seq: int | None = None
    last_rule_id: str | None = None


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError) as exc:
        raise InvalidParameterError("cursor segment is not valid base64url") from exc


def _mac(payload_b64: str, key: bytes) -> bytes:
    signing_input = (CURSOR_SIGNING_LABEL + "." + payload_b64).encode("ascii")
    return hmac.new(key, signing_input, hashlib.sha256).digest()


def encode_cursor(state: CursorState, key: bytes) -> str:
    payload = json.dumps(
        _as_dict(state),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    body = _b64url_encode(payload.encode("ascii"))
    return CURSOR_PREFIX + "." + body + "." + _b64url_encode(_mac(body, key))


def decode_cursor(cursor: str, key: bytes) -> CursorState:
    if not isinstance(cursor, str) or not cursor:
        raise InvalidParameterError("missing cursor")
    if len(cursor) > MAX_CURSOR_BYTES:
        raise InvalidParameterError("cursor exceeds the size limit")
    try:
        cursor.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InvalidParameterError("cursor is not ASCII") from exc

    parts = cursor.split(".")
    if len(parts) != 3:
        raise InvalidParameterError("cursor must have three segments")
    prefix, payload_b64, sig_b64 = parts
    if prefix != CURSOR_PREFIX:
        raise InvalidParameterError("unknown cursor scheme")

    signature = _b64url_decode(sig_b64)
    if len(signature) != 32:
        raise InvalidParameterError("cursor signature is not a 32-byte digest")
    if not hmac.compare_digest(signature, _mac(payload_b64, key)):
        raise InvalidParameterError("cursor signature does not match")

    payload_bytes = _b64url_decode(payload_b64)
    try:
        payload_text = payload_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise InvalidParameterError("cursor payload is not ASCII") from exc
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise InvalidParameterError("cursor payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidParameterError("cursor payload must be an object")

    unknown = set(payload) - _CURSOR_KEYS
    if unknown:
        raise InvalidParameterError("cursor payload contains unknown keys")
    try:
        return _from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidParameterError("cursor payload is malformed") from exc


def _as_dict(state: CursorState) -> dict:
    return {
        "v": CURSOR_VERSION,
        "actor_ref": state.actor_ref,
        "policy_version": state.policy_version,
        "query": state.query,
        "run_id": state.run_id,
        "device_id": state.device_id,
        "page_size": state.page_size,
        "window_high_water": state.window_high_water,
        "alarm_high_water_start_seq": state.alarm_high_water_start_seq,
        "alarm_high_water_rule_id": state.alarm_high_water_rule_id,
        "last_start_seq": state.last_start_seq,
        "last_rule_id": state.last_rule_id,
    }


def _from_dict(payload: dict) -> CursorState:
    if payload.get("v") != CURSOR_VERSION:
        raise InvalidParameterError("unsupported cursor version")
    query = payload["query"]
    if query not in _QUERY_KINDS:
        raise InvalidParameterError("unknown cursor query kind")
    for name in ("actor_ref", "policy_version", "run_id", "device_id"):
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise InvalidParameterError("cursor field %s is invalid" % name)
    page_size = payload.get("page_size")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
        raise InvalidParameterError("cursor page_size is invalid")

    def _opt_int(name):
        value = payload.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidParameterError("cursor field %s is invalid" % name)
        return value

    def _opt_str(name):
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise InvalidParameterError("cursor field %s is invalid" % name)
        return value

    window_high_water = _opt_int("window_high_water")
    alarm_high_water_start_seq = _opt_int("alarm_high_water_start_seq")
    alarm_high_water_rule_id = _opt_str("alarm_high_water_rule_id")
    last_start_seq = _opt_int("last_start_seq")
    last_rule_id = _opt_str("last_rule_id")

    if query == QUERY_WINDOWS:
        if window_high_water is None:
            raise InvalidParameterError("windows cursor lacks a high-water mark")
    else:
        if alarm_high_water_start_seq is None or alarm_high_water_rule_id is None:
            raise InvalidParameterError("alarms cursor lacks a high-water mark")

    return CursorState(
        actor_ref=payload["actor_ref"],
        policy_version=payload["policy_version"],
        query=query,
        run_id=payload["run_id"],
        device_id=payload["device_id"],
        page_size=page_size,
        window_high_water=window_high_water,
        alarm_high_water_start_seq=alarm_high_water_start_seq,
        alarm_high_water_rule_id=alarm_high_water_rule_id,
        last_start_seq=last_start_seq,
        last_rule_id=last_rule_id,
    )
