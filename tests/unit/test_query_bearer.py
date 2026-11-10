"""Unit tests for the HMAC bearer (ADR-0007 section 5.1.2).

The token is the three-segment project format g15q1.claims.sig, not a JWT.
Every verification failure must map to the one undifferentiated 401 family,
so these tests assert the exception type and never the message the caller
sees, which is internal-only.
"""

from __future__ import annotations

import base64
import json

import pytest

from g15.query.bearer import (
    BEARER_PREFIX,
    CLAIM_TYP,
    CLAIM_VER,
    MAX_TOKEN_BYTES,
    sign_token,
    verify_token,
)
from g15.query.errors import AuthenticationError

KEY = b"k" * 32
ISSUER = "issuer"
AUDIENCE = "audience"


def _claims(**overrides):
    base_ = {
        "typ": CLAIM_TYP,
        "ver": CLAIM_VER,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "s1",
        "jti": "j1",
        "iat": 1000,
        "exp": 1600,
    }
    base_.update(overrides)
    return base_


def _token(**overrides):
    return sign_token(_claims(**overrides), KEY)


def _verify(token, now=1500):
    return verify_token(token, KEY, issuer=ISSUER, audience=AUDIENCE, now=now)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_round_trip_returns_subject():
    subject = _verify(_token())
    assert subject.sub == "s1"
    assert subject.jti == "j1"
    assert subject.iat == 1000
    assert subject.exp == 1600


def test_sign_rejects_short_key():
    with pytest.raises(ValueError):
        sign_token(_claims(), b"short")


def test_missing_token():
    with pytest.raises(AuthenticationError):
        _verify("")


def test_oversized_token():
    with pytest.raises(AuthenticationError):
        _verify("a" * (MAX_TOKEN_BYTES + 1))


def test_non_ascii_token():
    with pytest.raises(AuthenticationError):
        _verify("g15q1.\u00e9.abc")


def test_not_three_segments():
    with pytest.raises(AuthenticationError):
        _verify("g15q1.onlytwo")


def test_wrong_prefix():
    token = _token()
    _, body, sig = token.split(".")
    with pytest.raises(AuthenticationError):
        _verify("evil." + body + "." + sig)


def test_signature_wrong_length():
    _, body, _sig = _token().split(".")
    with pytest.raises(AuthenticationError):
        _verify(BEARER_PREFIX + "." + body + "." + _b64(b"x" * 16))


def test_signature_mismatch():
    _, body, _sig = _token().split(".")
    wrong = _b64(b"y" * 32)
    with pytest.raises(AuthenticationError):
        _verify(BEARER_PREFIX + "." + body + "." + wrong)


def test_duplicate_claim_key_rejected():
    payload = '{"typ":"g15-query-access","typ":"g15-query-access"}'
    body = _b64(payload.encode())
    import hmac
    import hashlib

    mac = hmac.new(KEY, (BEARER_PREFIX + "." + body).encode(), hashlib.sha256).digest()
    with pytest.raises(AuthenticationError):
        _verify(BEARER_PREFIX + "." + body + "." + _b64(mac))


def test_unknown_claim_key_rejected():
    with pytest.raises(AuthenticationError):
        _verify(_token(extra="nope"))


def test_wrong_typ_and_ver():
    with pytest.raises(AuthenticationError):
        _verify(_token(typ="other"))
    with pytest.raises(AuthenticationError):
        _verify(_token(ver="9"))


def test_wrong_issuer_and_audience():
    with pytest.raises(AuthenticationError):
        _verify(_token(iss="other"))
    with pytest.raises(AuthenticationError):
        _verify(_token(aud="other"))


def test_empty_subject_rejected():
    with pytest.raises(AuthenticationError):
        _verify(_token(sub=""))


def test_missing_subject_rejected():
    claims = _claims()
    del claims["sub"]
    with pytest.raises(AuthenticationError):
        _verify(sign_token(claims, KEY))


def test_exp_not_after_iat():
    with pytest.raises(AuthenticationError):
        _verify(_token(iat=1600, exp=1600))


def test_ttl_exceeds_hard_cap():
    with pytest.raises(AuthenticationError):
        _verify(_token(iat=1000, exp=2000))


def test_expired_token():
    with pytest.raises(AuthenticationError):
        _verify(_token(iat=1000, exp=1600), now=2000)


def test_future_issued_token():
    with pytest.raises(AuthenticationError):
        _verify(_token(iat=1000, exp=1600), now=100)


def test_non_integer_iat_exp():
    with pytest.raises(AuthenticationError):
        _verify(_token(iat="1000"))
    with pytest.raises(AuthenticationError):
        _verify(_token(exp=1600.5))
