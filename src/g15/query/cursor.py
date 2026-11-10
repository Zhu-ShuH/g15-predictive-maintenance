"""Integrity-protected, scope-bound keyset cursors for query pagination."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

CURSOR_PREFIX = "g15c1"
CURSOR_VERSION = "1"
MAX_CURSOR_BYTES = 4096
WINDOW_SORT = "start_seq_desc"
ALARM_SORT = "start_seq_desc_rule_id_desc"

_BASE64URL = re.compile(r"[A-Za-z0-9_-]+\Z")
_FIELDS = frozenset(
    {
        "cursor_version",
        "actor_ref",
        "policy_version",
        "permission_domain",
        "query_type",
        "run_id",
        "device_id",
        "sort",
        "page_size",
        "high_water",
        "last_key",
    }
)


class CursorError(ValueError):
    """Raised when a cursor is malformed or its integrity check fails."""


class CursorScopeError(CursorError):
    """Raised when a valid cursor belongs to another authorization scope."""


@dataclass(frozen=True)
class CursorScope:
    """Every request property to which a cursor is cryptographically bound."""

    actor_ref: str
    policy_version: str
    permission_domain: str
    query_type: str
    run_id: str
    device_id: str
    sort: str
    page_size: int


@dataclass(frozen=True)
class CursorState:
    """Decoded high-water and last-emitted key for one result stream."""

    scope: CursorScope
    high_water: int | tuple[int, str]
    last_key: int | tuple[int, str]


@dataclass(frozen=True)
class CursorCodec:
    """Encode and authenticate cursors with a runtime-only purpose key."""

    key: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.key, bytes) or len(self.key) < 32:
            raise ValueError("cursor key must be at least 32 runtime-injected bytes")

    def encode(self, state: CursorState) -> str:
        """Return a canonical, unpadded base64url cursor."""
        _validate_state(state)
        payload: dict[str, object] = {
            "actor_ref": state.scope.actor_ref,
            "cursor_version": CURSOR_VERSION,
            "device_id": state.scope.device_id,
            "high_water": _json_key(state.high_water),
            "last_key": _json_key(state.last_key),
            "page_size": state.scope.page_size,
            "permission_domain": state.scope.permission_domain,
            "policy_version": state.scope.policy_version,
            "query_type": state.scope.query_type,
            "run_id": state.scope.run_id,
            "sort": state.scope.sort,
        }
        payload_segment = _encode_base64url(_canonical_json(payload))
        signing_input = f"{CURSOR_PREFIX}.{payload_segment}"
        signature = hmac.new(
            self.key, signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        cursor = f"{signing_input}.{_encode_base64url(signature)}"
        if len(cursor.encode("ascii")) > MAX_CURSOR_BYTES:
            raise CursorError("invalid cursor")
        return cursor

    def decode(self, cursor: str, *, expected: CursorScope) -> CursorState:
        """Validate integrity and require an exact current authorization scope."""
        if not isinstance(cursor, str):
            raise CursorError("invalid cursor")
        try:
            raw = cursor.encode("ascii")
        except UnicodeEncodeError as exc:
            raise CursorError("invalid cursor") from exc
        if not raw or len(raw) > MAX_CURSOR_BYTES:
            raise CursorError("invalid cursor")
        segments = cursor.split(".")
        if len(segments) != 3 or segments[0] != CURSOR_PREFIX:
            raise CursorError("invalid cursor")
        payload_segment, signature_segment = segments[1], segments[2]
        signature = _decode_base64url(signature_segment)
        if len(signature) != hashlib.sha256().digest_size:
            raise CursorError("invalid cursor")
        signing_input = f"{CURSOR_PREFIX}.{payload_segment}".encode("ascii")
        expected_signature = hmac.new(self.key, signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise CursorError("invalid cursor")

        payload = _strict_json_object(_decode_base64url(payload_segment))
        try:
            canonical_payload = _canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise CursorError("invalid cursor") from exc
        if _encode_base64url(canonical_payload) != payload_segment:
            raise CursorError("invalid cursor")
        if set(payload) != _FIELDS or payload["cursor_version"] != CURSOR_VERSION:
            raise CursorError("invalid cursor")
        actual_scope = _scope_from_payload(payload)
        if actual_scope != expected:
            raise CursorScopeError("cursor belongs to another query scope")
        state = CursorState(
            scope=actual_scope,
            high_water=_key_from_json(payload["high_water"], actual_scope.query_type),
            last_key=_key_from_json(payload["last_key"], actual_scope.query_type),
        )
        _validate_state(state)
        return state


def _scope_from_payload(payload: dict[str, object]) -> CursorScope:
    """Decode only primitive cursor scope fields."""
    text_fields = (
        "actor_ref",
        "policy_version",
        "permission_domain",
        "query_type",
        "run_id",
        "device_id",
        "sort",
    )
    for name in text_fields:
        value = payload[name]
        if not isinstance(value, str) or not value:
            raise CursorError("invalid cursor")
    page_size = payload["page_size"]
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise CursorError("invalid cursor")
    return CursorScope(
        actor_ref=str(payload["actor_ref"]),
        policy_version=str(payload["policy_version"]),
        permission_domain=str(payload["permission_domain"]),
        query_type=str(payload["query_type"]),
        run_id=str(payload["run_id"]),
        device_id=str(payload["device_id"]),
        sort=str(payload["sort"]),
        page_size=page_size,
    )


def _validate_state(state: CursorState) -> None:
    scope = state.scope
    if not 1 <= scope.page_size <= 100:
        raise CursorError("invalid cursor")
    if scope.query_type == "windows":
        if scope.sort != WINDOW_SORT:
            raise CursorError("invalid cursor")
        high = _plain_nonnegative_int(state.high_water)
        last = _plain_nonnegative_int(state.last_key)
        if last > high:
            raise CursorError("invalid cursor")
        return
    if scope.query_type == "alarms":
        if scope.sort != ALARM_SORT:
            raise CursorError("invalid cursor")
        high = _alarm_key(state.high_water)
        last = _alarm_key(state.last_key)
        if last > high:
            raise CursorError("invalid cursor")
        return
    raise CursorError("invalid cursor")


def _key_from_json(value: object, query_type: str) -> int | tuple[int, str]:
    if query_type == "windows":
        return _plain_nonnegative_int(value)
    if query_type == "alarms":
        if not isinstance(value, list) or len(value) != 2:
            raise CursorError("invalid cursor")
        return _alarm_key((value[0], value[1]))
    raise CursorError("invalid cursor")


def _json_key(value: int | tuple[int, str]) -> int | list[object]:
    if isinstance(value, tuple):
        return [value[0], value[1]]
    return value


def _alarm_key(value: object) -> tuple[int, str]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise CursorError("invalid cursor")
    start_seq = _plain_nonnegative_int(value[0])
    rule_id = value[1]
    if not isinstance(rule_id, str) or not rule_id:
        raise CursorError("invalid cursor")
    return (start_seq, rule_id)


def _plain_nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CursorError("invalid cursor")
    return value


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _strict_json_object(payload: bytes) -> dict[str, object]:
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CursorError("invalid cursor")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise CursorError("invalid cursor") from exc
    if not isinstance(value, dict):
        raise CursorError("invalid cursor")
    return value


def _encode_base64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_base64url(segment: str) -> bytes:
    if not segment or "=" in segment or _BASE64URL.fullmatch(segment) is None:
        raise CursorError("invalid cursor")
    padding = "=" * (-len(segment) % 4)
    try:
        payload = base64.b64decode(
            (segment + padding).encode("ascii"), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise CursorError("invalid cursor") from exc
    if _encode_base64url(payload) != segment:
        raise CursorError("invalid cursor")
    return payload
