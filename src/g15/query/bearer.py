"""HMAC-SHA-256 bearer verification for the D0 query subset.

Implements the token format in ADR-0007 section 5.1.2: a project-specific
three-segment bearer g15q1.<claims-b64url>.<sig-b64url> that is deliberately
not a JWT. The MAC is verified with hmac.compare_digest before any claim is
parsed; everything before that point is untrusted input.

The signing function exists only for the internal bootstrap/test fixture
(5.1.1 point 2); there is no network login or token-issuing endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from g15.query.errors import AuthenticationError

BEARER_PREFIX = "g15q1"
CLAIM_TYP = "g15-query-access"
CLAIM_VER = "1"
ALLOWED_CLAIMS = frozenset({"typ", "ver", "iss", "aud", "sub", "iat", "exp", "jti"})
MAX_TOKEN_BYTES = 4096
TTL_DEFAULT_SECONDS = 600
TTL_MAX_SECONDS = 900
CLOCK_TOLERANCE_SECONDS = 30
_MAX_IDENTIFIER_BYTES = 128


@dataclass(frozen=True)
class AuthenticatedSubject:
    """The authenticated identity extracted from a verified bearer."""

    sub: str
    jti: str
    iat: int
    exp: int


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError) as exc:
        raise AuthenticationError("bearer segment is not valid base64url") from exc


def sign_token(claims: dict, key: bytes) -> str:
    """Sign claims into a bearer token.

    Internal signer for the bootstrap fixture and tests only; never exposed
    behind a network endpoint. key must be at least 32 bytes of high-entropy
    material supplied at runtime; it is never read from the repository, YAML,
    a default value or this module's environment.

    Raises:
        ValueError: if key is shorter than 32 bytes or claims cannot be
            encoded by the canonical serializer.
    """
    if len(key) < 32:
        raise ValueError("signing key must be at least 32 bytes")
    payload = json.dumps(
        claims,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    body = _b64url_encode(payload.encode("ascii"))
    signing_input = (BEARER_PREFIX + "." + body).encode("ascii")
    mac = hmac.new(key, signing_input, hashlib.sha256).digest()
    return BEARER_PREFIX + "." + body + "." + _b64url_encode(mac)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuthenticationError("bearer claims contain a duplicate key")
        result[key] = value
    return result


def verify_token(token, key, *, issuer, audience, now):
    """Verify a bearer and return the authenticated subject.

    Check order mirrors 5.1.2: structure and character set, then the fixed
    prefix, then the 32-byte signature, then the MAC, and only then the JSON
    claims. Nothing parsed before the MAC check is trusted.

    Raises:
        AuthenticationError: for every failure, so the transport maps the whole
            family to a single undifferentiated 401.
    """
    if not isinstance(token, str) or not token:
        raise AuthenticationError("missing bearer token")
    if len(token) > MAX_TOKEN_BYTES:
        raise AuthenticationError("bearer token exceeds the size limit")
    try:
        token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AuthenticationError("bearer token is not ASCII") from exc

    parts = token.split(".")
    if len(parts) != 3:
        raise AuthenticationError("bearer must have three segments")
    prefix, claims_b64, sig_b64 = parts
    if prefix != BEARER_PREFIX:
        raise AuthenticationError("unknown bearer scheme")

    signature = _b64url_decode(sig_b64)
    if len(signature) != 32:
        raise AuthenticationError("signature is not a 32-byte HMAC-SHA-256 digest")

    signing_input = (BEARER_PREFIX + "." + claims_b64).encode("ascii")
    expected = hmac.new(key, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise AuthenticationError("signature does not match")

    claims_bytes = _b64url_decode(claims_b64)
    try:
        claims_text = claims_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AuthenticationError("claims are not ASCII") from exc
    try:
        claims = json.loads(claims_text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise AuthenticationError("claims are not valid JSON") from exc
    if not isinstance(claims, dict):
        raise AuthenticationError("claims must be a JSON object")

    canonical = json.dumps(
        claims,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    if canonical.encode("ascii") != claims_bytes:
        raise AuthenticationError("claims are not canonical base64url")

    unknown = set(claims) - ALLOWED_CLAIMS
    if unknown:
        raise AuthenticationError("claims contain unknown keys")
    if claims.get("typ") != CLAIM_TYP:
        raise AuthenticationError("wrong token typ")
    if claims.get("ver") != CLAIM_VER:
        raise AuthenticationError("wrong token ver")
    if claims.get("iss") != issuer:
        raise AuthenticationError("wrong token issuer")
    if claims.get("aud") != audience:
        raise AuthenticationError("wrong token audience")

    for name in ("sub", "jti"):
        value = claims.get(name)
        if not isinstance(value, str) or not value:
            raise AuthenticationError("claim %s is not a valid identifier" % name)
        if len(value.encode("ascii")) > _MAX_IDENTIFIER_BYTES:
            raise AuthenticationError("claim %s is not a valid identifier" % name)

    iat = claims.get("iat")
    exp = claims.get("exp")
    if isinstance(iat, bool) or isinstance(exp, bool):
        raise AuthenticationError("iat/exp must be integers")
    if not isinstance(iat, int) or not isinstance(exp, int):
        raise AuthenticationError("iat/exp must be integers")
    if exp <= iat:
        raise AuthenticationError("exp must be later than iat")
    if exp - iat > TTL_MAX_SECONDS:
        raise AuthenticationError("token TTL exceeds the hard cap")
    if now > exp + CLOCK_TOLERANCE_SECONDS:
        raise AuthenticationError("token is expired")
    if iat > now + CLOCK_TOLERANCE_SECONDS:
        raise AuthenticationError("token was issued in the future")

    return AuthenticatedSubject(sub=claims["sub"], jti=claims["jti"], iat=iat, exp=exp)
