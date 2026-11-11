"""Boundary tests for the frozen #135 bearer and cursor contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import replace

import pytest

from g15.query.auth import (
    MAX_TOKEN_BYTES,
    TOKEN_PREFIX,
    TOKEN_TYPE,
    TOKEN_VERSION,
    TokenError,
    TokenVerifier,
    issue_token,
)
from g15.query.cursor import (
    MAX_CURSOR_BYTES,
    WINDOW_SORT,
    CursorCodec,
    CursorError,
    CursorScope,
    CursorScopeError,
    CursorState,
)

ISSUER = "issuer-contract"
AUDIENCE = "audience-contract"
SUBJECT = "subject-contract"
TOKEN_ID = "token-contract-id-000001"


def _base_claims() -> dict[str, object]:
    return {
        "aud": AUDIENCE,
        "exp": 1_600,
        "iat": 1_000,
        "iss": ISSUER,
        "jti": TOKEN_ID,
        "sub": SUBJECT,
        "typ": TOKEN_TYPE,
        "ver": TOKEN_VERSION,
    }


def _sign_claims(key: bytes, claims: dict[str, object]) -> str:
    payload = json.dumps(
        claims,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    payload_segment = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return _sign_payload_segment(key, payload_segment)


def _sign_payload_segment(key: bytes, payload_segment: str) -> str:
    signing_input = f"{TOKEN_PREFIX}.{payload_segment}"
    signature = hmac.new(key, signing_input.encode("ascii"), hashlib.sha256).digest()
    signature_segment = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{signing_input}.{signature_segment}"


def _verifier(key: bytes, *, now: int = 1_100) -> TokenVerifier:
    return TokenVerifier(
        key=key,
        issuer=ISSUER,
        audience=AUDIENCE,
        clock=lambda: now,
    )


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "now", "accepted"),
    [
        pytest.param(1_000, 1_100, 970, True, id="future-iat-at-skew"),
        pytest.param(1_000, 1_100, 969, False, id="future-iat-beyond-skew"),
        pytest.param(1_000, 1_100, 1_130, True, id="expired-at-skew"),
        pytest.param(1_000, 1_100, 1_131, False, id="expired-beyond-skew"),
        pytest.param(1_000, 1_000, 1_000, False, id="exp-equals-iat"),
        pytest.param(1_000, 999, 1_000, False, id="exp-before-iat"),
        pytest.param(1_000, 1_900, 1_000, True, id="ttl-at-hard-limit"),
        pytest.param(1_000, 1_901, 1_000, False, id="ttl-over-hard-limit"),
    ],
)
def test_token_time_boundaries_are_deterministic(
    issued_at: int,
    expires_at: int,
    now: int,
    accepted: bool,
) -> None:
    key = secrets.token_bytes(32)
    claims = _base_claims()
    claims.update({"iat": issued_at, "exp": expires_at})
    token = _sign_claims(key, claims)

    if accepted:
        verified = _verifier(key, now=now).verify(token)
        assert verified.issued_at == issued_at
        assert verified.expires_at == expires_at
    else:
        with pytest.raises(TokenError):
            _verifier(key, now=now).verify(token)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        pytest.param("typ", "another-token-type", id="wrong-type"),
        pytest.param("ver", "2", id="wrong-version"),
        pytest.param("iss", "another-issuer", id="wrong-issuer"),
        pytest.param("aud", "another-audience", id="wrong-audience"),
        pytest.param("sub", "", id="empty-subject"),
    ],
)
def test_signed_tokens_with_invalid_identity_claims_are_rejected(
    field: str, invalid_value: object
) -> None:
    key = secrets.token_bytes(32)
    claims = _base_claims()
    claims[field] = invalid_value

    with pytest.raises(TokenError):
        _verifier(key).verify(_sign_claims(key, claims))


@pytest.mark.parametrize("missing_field", sorted(_base_claims()))
def test_every_required_claim_is_mandatory(missing_field: str) -> None:
    key = secrets.token_bytes(32)
    claims = _base_claims()
    del claims[missing_field]

    with pytest.raises(TokenError):
        _verifier(key).verify(_sign_claims(key, claims))


def test_token_rejects_padding_and_input_over_the_byte_limit() -> None:
    key = secrets.token_bytes(32)
    token = issue_token(
        key=key,
        issuer=ISSUER,
        audience=AUDIENCE,
        subject=SUBJECT,
        now=1_000,
    )
    prefix, payload, signature = token.split(".")
    padded_payload_token = _sign_payload_segment(key, payload + "=")
    padded_signature_token = f"{prefix}.{payload}.{signature}="
    overlong_token = "x" * (MAX_TOKEN_BYTES + 1)

    for malformed in (
        padded_payload_token,
        padded_signature_token,
        overlong_token,
    ):
        with pytest.raises(TokenError):
            _verifier(key).verify(malformed)


def _window_scope() -> CursorScope:
    return CursorScope(
        actor_ref="actor-contract",
        policy_version="policy-contract-1",
        permission_domain="plant-contract",
        query_type="windows",
        run_id="run-contract",
        device_id="device-contract",
        sort=WINDOW_SORT,
        page_size=25,
    )


@pytest.mark.parametrize(
    ("field", "other_value"),
    [
        pytest.param("policy_version", "policy-contract-2", id="policy"),
        pytest.param("permission_domain", "plant-other", id="domain"),
        pytest.param("query_type", "alarms", id="query"),
        pytest.param("run_id", "run-other", id="run"),
        pytest.param("device_id", "device-other", id="device"),
        pytest.param("sort", "another-sort", id="sort"),
        pytest.param("page_size", 26, id="page-size"),
    ],
)
def test_cursor_is_bound_to_every_non_actor_scope_field(
    field: str, other_value: object
) -> None:
    codec = CursorCodec(secrets.token_bytes(32))
    scope = _window_scope()
    cursor = codec.encode(CursorState(scope=scope, high_water=100, last_key=75))
    other_scope = replace(scope, **{field: other_value})

    with pytest.raises(CursorScopeError):
        codec.decode(cursor, expected=other_scope)


def test_cursor_rejects_payload_tampering_wrong_key_and_oversized_input() -> None:
    codec = CursorCodec(secrets.token_bytes(32))
    scope = _window_scope()
    cursor = codec.encode(CursorState(scope=scope, high_water=100, last_key=75))
    prefix, payload, signature = cursor.split(".")
    tampered_payload = ("A" if payload[0] != "A" else "B") + payload[1:]
    oversized = "x" * (MAX_CURSOR_BYTES + 1)

    with pytest.raises(CursorError):
        codec.decode(f"{prefix}.{tampered_payload}.{signature}", expected=scope)
    with pytest.raises(CursorError):
        CursorCodec(secrets.token_bytes(32)).decode(cursor, expected=scope)
    with pytest.raises(CursorError):
        codec.decode(oversized, expected=scope)
