"""Unit tests for the #135 bearer, policy, and cursor contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

import pytest

from g15.query.auth import (
    AuthorizationPolicy,
    PolicyError,
    TokenError,
    TokenVerifier,
    issue_token,
)
from g15.query.cursor import (
    ALARM_SORT,
    WINDOW_SORT,
    CursorCodec,
    CursorError,
    CursorScope,
    CursorScopeError,
    CursorState,
)


def _policy_text(subjects: str) -> str:
    return (
        'authz_schema_version: "1"\n'
        'policy_version: "policy-test-1"\n'
        "subjects:\n"
        f"{subjects}"
    )


def _one_subject_policy() -> str:
    return _policy_text(
        "  subject-1:\n"
        "    actor_ref: actor-1\n"
        "    permission_domain: plant-a\n"
        "    allow:\n"
        "      - run_id: run-1\n"
        "        device_id: device-1\n"
    )


def _sign_raw_payload(key: bytes, payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signing_input = f"g15q1.{encoded}"
    signature = hmac.new(key, signing_input.encode("ascii"), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{signing_input}.{encoded_signature}"


def test_short_lived_token_round_trip_and_replay_are_explicit() -> None:
    key = secrets.token_bytes(32)
    token = issue_token(
        key=key,
        issuer="issuer-test",
        audience="query-test",
        subject="subject-1",
        now=1_000,
        ttl_seconds=600,
    )
    verifier = TokenVerifier(
        key=key,
        issuer="issuer-test",
        audience="query-test",
        clock=lambda: 1_100,
    )

    first = verifier.verify(token)
    second = verifier.verify(token)

    assert first == second
    assert first.subject == "subject-1"


def test_token_id_accepts_urlsafe_randomness_with_a_leading_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = secrets.token_bytes(32)
    random_segment = "_0123456789abcdefghijklmn"
    monkeypatch.setattr(
        "g15.query.auth.secrets.token_urlsafe", lambda _length: random_segment
    )

    token = issue_token(
        key=key,
        issuer="issuer-test",
        audience="query-test",
        subject="subject-1",
        now=1_000,
    )
    claims = TokenVerifier(
        key=key,
        issuer="issuer-test",
        audience="query-test",
        clock=lambda: 1_100,
    ).verify(token)

    assert claims.token_id == f"jti-{random_segment}"


@pytest.mark.parametrize("mutation", ["payload", "signature"])
def test_token_tampering_is_rejected(mutation: str) -> None:
    key = secrets.token_bytes(32)
    token = issue_token(
        key=key,
        issuer="issuer-test",
        audience="query-test",
        subject="subject-1",
        now=1_000,
    )
    pieces = token.split(".")
    index = 1 if mutation == "payload" else 2
    pieces[index] = ("A" if pieces[index][0] != "A" else "B") + pieces[index][1:]

    with pytest.raises(TokenError):
        TokenVerifier(
            key=key,
            issuer="issuer-test",
            audience="query-test",
            clock=lambda: 1_100,
        ).verify(".".join(pieces))


def test_expired_token_and_wrong_runtime_key_are_rejected() -> None:
    key = secrets.token_bytes(32)
    token = issue_token(
        key=key,
        issuer="issuer-test",
        audience="query-test",
        subject="subject-1",
        now=1_000,
        ttl_seconds=60,
    )

    with pytest.raises(TokenError):
        TokenVerifier(
            key=key,
            issuer="issuer-test",
            audience="query-test",
            clock=lambda: 1_091,
        ).verify(token)
    with pytest.raises(TokenError):
        TokenVerifier(
            key=secrets.token_bytes(32),
            issuer="issuer-test",
            audience="query-test",
            clock=lambda: 1_010,
        ).verify(token)


@pytest.mark.parametrize("variant", ["duplicate", "unknown", "noncanonical"])
def test_signed_but_noncanonical_or_unexpected_claims_are_rejected(
    variant: str,
) -> None:
    key = secrets.token_bytes(32)
    claims = {
        "aud": "query-test",
        "exp": 1_600,
        "iat": 1_000,
        "iss": "issuer-test",
        "jti": "jti-test-00000004",
        "sub": "subject-1",
        "typ": "g15-query-access",
        "ver": "1",
    }
    if variant == "duplicate":
        payload = (
            b'{"aud":"query-test","aud":"query-test","exp":1600,'
            b'"iat":1000,"iss":"issuer-test","jti":"jti-test-00000004",'
            b'"sub":"subject-1","typ":"g15-query-access","ver":"1"}'
        )
    else:
        if variant == "unknown":
            claims["role"] = "admin"
        payload = json.dumps(
            claims,
            sort_keys=True,
            separators=(", ", ": ") if variant == "noncanonical" else (",", ":"),
        ).encode("ascii")
    token = _sign_raw_payload(key, payload)

    with pytest.raises(TokenError):
        TokenVerifier(
            key=key,
            issuer="issuer-test",
            audience="query-test",
            clock=lambda: 1_100,
        ).verify(token)


def test_policy_authorizes_only_the_exact_pair() -> None:
    policy = AuthorizationPolicy.from_yaml(_one_subject_policy())

    grant = policy.authorize("subject-1", "run-1", "device-1")

    assert grant is not None
    assert grant.actor_ref == "actor-1"
    assert policy.authorize("subject-1", "run-2", "device-1") is None
    assert policy.authorize("SUBJECT-1", "run-1", "device-1") is None


@pytest.mark.parametrize(
    "document",
    [
        _policy_text(
            "  subject-1:\n"
            "    actor_ref: actor-1\n"
            "    actor_ref: actor-2\n"
            "    permission_domain: plant-a\n"
            "    allow: [{run_id: run-1, device_id: device-1}]\n"
        ),
        _policy_text(
            "  subject-1: &grant\n"
            "    actor_ref: actor-1\n"
            "    permission_domain: plant-a\n"
            "    allow: [{run_id: run-1, device_id: device-1}]\n"
            "  subject-2: *grant\n"
        ),
        _policy_text(
            "  subject-1:\n"
            "    actor_ref: actor-1\n"
            "    permission_domain: plant-a\n"
            "    allow: [{run_id: '../escape', device_id: device-1}]\n"
        ),
    ],
)
def test_policy_rejects_duplicates_aliases_and_paths(document: str) -> None:
    with pytest.raises(PolicyError):
        AuthorizationPolicy.from_yaml(document)


def test_policy_rejects_more_than_ten_authorized_devices() -> None:
    resources = "".join(
        f"      - run_id: run-{index}\n        device_id: device-{index}\n"
        for index in range(11)
    )
    document = _policy_text(
        "  subject-1:\n"
        "    actor_ref: actor-1\n"
        "    permission_domain: plant-a\n"
        "    allow:\n"
        f"{resources}"
    )

    with pytest.raises(PolicyError):
        AuthorizationPolicy.from_yaml(document)


def test_policy_rejects_actor_ref_matching_any_subject_case_insensitively() -> None:
    document = _policy_text(
        "  subject-1:\n"
        "    actor_ref: SUBJECT-2\n"
        "    permission_domain: plant-a\n"
        "    allow: [{run_id: run-1, device_id: device-1}]\n"
        "  subject-2:\n"
        "    actor_ref: actor-2\n"
        "    permission_domain: plant-a\n"
        "    allow: [{run_id: run-2, device_id: device-2}]\n"
    )

    with pytest.raises(PolicyError):
        AuthorizationPolicy.from_yaml(document)


def test_cursor_is_integrity_protected_and_bound_to_actor_scope() -> None:
    codec = CursorCodec(secrets.token_bytes(32))
    scope = CursorScope(
        actor_ref="actor-1",
        policy_version="policy-test-1",
        permission_domain="plant-a",
        query_type="windows",
        run_id="run-1",
        device_id="device-1",
        sort=WINDOW_SORT,
        page_size=2,
    )
    cursor = codec.encode(CursorState(scope=scope, high_water=50, last_key=40))

    assert codec.decode(cursor, expected=scope).last_key == 40
    other_actor = CursorScope(
        **{**scope.__dict__, "actor_ref": "actor-2"},
    )
    with pytest.raises(CursorScopeError):
        codec.decode(cursor, expected=other_actor)
    with pytest.raises(CursorError):
        codec.decode(cursor[:-1] + ("A" if cursor[-1] != "A" else "B"), expected=scope)


def test_cursor_encoder_refuses_a_token_its_decoder_would_reject() -> None:
    codec = CursorCodec(secrets.token_bytes(32))
    scope = CursorScope(
        actor_ref="actor-1",
        policy_version="policy-test-1",
        permission_domain="plant-a",
        query_type="alarms",
        run_id="run-1",
        device_id="device-1",
        sort=ALARM_SORT,
        page_size=2,
    )
    oversized_rule_id = "rule-" + ("x" * 4_096)

    with pytest.raises(CursorError):
        codec.encode(
            CursorState(
                scope=scope,
                high_water=(50, oversized_rule_id),
                last_key=(40, oversized_rule_id),
            )
        )


def test_runtime_keys_must_be_at_least_32_bytes() -> None:
    with pytest.raises(ValueError):
        TokenVerifier(key=b"short", issuer="issuer", audience="audience")
    with pytest.raises(ValueError):
        CursorCodec(b"short")
