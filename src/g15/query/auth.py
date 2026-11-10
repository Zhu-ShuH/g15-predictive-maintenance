"""Strict bearer authentication and static D0 authorization policy loading.

The custom ``g15q1`` token is intentionally small and fixed-purpose. It is not
JWT: clients cannot select an algorithm and the service accepts only a complete
HMAC-SHA-256 signature. The YAML document establishes authorization only; it
never proves identity and must not contain passwords or keys.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from yaml.events import AliasEvent, CollectionStartEvent, ScalarEvent

TOKEN_PREFIX = "g15q1"
TOKEN_TYPE = "g15-query-access"
TOKEN_VERSION = "1"
MAX_TOKEN_BYTES = 4096
DEFAULT_TOKEN_TTL_SECONDS = 600
MAX_TOKEN_TTL_SECONDS = 900
DEFAULT_CLOCK_SKEW_SECONDS = 30
MAX_AUTHORIZED_DEVICES = 10

_CLAIM_FIELDS = frozenset({"typ", "ver", "iss", "aud", "sub", "iat", "exp", "jti"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_RESOURCE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+\Z")


class TokenError(ValueError):
    """Raised for every externally indistinguishable bearer failure."""


class PolicyError(ValueError):
    """Raised when an authorization snapshot cannot be trusted as a whole."""


@dataclass(frozen=True)
class TokenClaims:
    """Trusted claims recovered after signature and canonical-form checks."""

    issuer: str
    audience: str
    subject: str
    issued_at: int
    expires_at: int
    token_id: str


@dataclass(frozen=True)
class ResourceGrant:
    """One exact run/device pair visible to a subject."""

    run_id: str
    device_id: str


@dataclass(frozen=True)
class SubjectGrant:
    """The non-PII audit identity and exact resources of one subject."""

    actor_ref: str
    permission_domain: str
    resources: frozenset[ResourceGrant]


@dataclass(frozen=True)
class AuthorizationPolicy:
    """Immutable authorization snapshot loaded fail-closed at startup."""

    policy_version: str
    subjects: Mapping[str, SubjectGrant]

    @classmethod
    def from_file(cls, path: Path | str) -> "AuthorizationPolicy":
        """Load and validate one UTF-8 YAML snapshot."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise PolicyError("authorization policy cannot be read") from exc
        return cls.from_yaml(text)

    @classmethod
    def from_yaml(cls, text: str) -> "AuthorizationPolicy":
        """Validate strict YAML without aliases, tags, merges, or duplicates."""
        if not isinstance(text, str) or not text.strip():
            raise PolicyError("authorization policy must be non-empty YAML")
        _reject_yaml_graph_features(text)
        try:
            payload = yaml.load(text, Loader=_StrictSafeLoader)
        except (yaml.YAMLError, PolicyError) as exc:
            raise PolicyError("authorization policy is invalid") from exc
        root = _mapping(payload, "policy")
        _exact_keys(
            root,
            {"authz_schema_version", "policy_version", "subjects"},
            "policy",
        )
        if root["authz_schema_version"] != "1":
            raise PolicyError("unsupported authz_schema_version")
        policy_version = _opaque_identifier(root["policy_version"], "policy_version")
        raw_subjects = _mapping(root["subjects"], "subjects")
        if not raw_subjects:
            raise PolicyError("subjects must not be empty")

        validated_subjects: list[tuple[str, object]] = []
        folded_subjects: set[str] = set()
        for raw_subject, raw_grant in raw_subjects.items():
            subject = _opaque_identifier(raw_subject, "subject")
            folded = subject.casefold()
            if folded in folded_subjects:
                raise PolicyError("subjects contain a case-ambiguous identifier")
            folded_subjects.add(folded)
            validated_subjects.append((subject, raw_grant))

        subjects: dict[str, SubjectGrant] = {}
        actor_refs: set[str] = set()
        for subject, raw_grant in validated_subjects:
            grant = _mapping(raw_grant, f"subjects[{subject!r}]")
            _exact_keys(
                grant,
                {"actor_ref", "permission_domain", "allow"},
                f"subjects[{subject!r}]",
            )
            actor_ref = _opaque_identifier(grant["actor_ref"], "actor_ref")
            folded_actor = actor_ref.casefold()
            if folded_actor in folded_subjects:
                raise PolicyError("actor_ref must not disclose a raw subject")
            if folded_actor in actor_refs:
                raise PolicyError("actor_ref values must be unique")
            actor_refs.add(folded_actor)
            permission_domain = _opaque_identifier(
                grant["permission_domain"], "permission_domain"
            )
            resources = _resource_grants(grant["allow"])
            subjects[subject] = SubjectGrant(
                actor_ref=actor_ref,
                permission_domain=permission_domain,
                resources=frozenset(resources),
            )
        return cls(
            policy_version=policy_version,
            subjects=MappingProxyType(subjects),
        )

    def authorize(
        self, subject: str, run_id: str, device_id: str
    ) -> SubjectGrant | None:
        """Return the exact grant, or ``None`` without fuzzy matching."""
        grant = self.subjects.get(subject)
        if grant is None:
            return None
        resource = ResourceGrant(run_id=run_id, device_id=device_id)
        return grant if resource in grant.resources else None


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that refuses duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _StrictSafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    """Construct a YAML mapping while treating duplicates as fatal."""
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise PolicyError("authorization mapping keys must be scalar") from exc
        if duplicate:
            raise PolicyError("authorization policy contains a duplicate key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _reject_yaml_graph_features(text: str) -> None:
    """Reject aliases and every explicitly written YAML tag."""
    try:
        for event in yaml.parse(text):
            if isinstance(event, AliasEvent):
                raise PolicyError("YAML aliases are not allowed")
            if isinstance(event, (ScalarEvent, CollectionStartEvent)):
                if event.tag is not None:
                    raise PolicyError("explicit YAML tags are not allowed")
            if isinstance(event, ScalarEvent) and event.value == "<<":
                raise PolicyError("YAML merge keys are not allowed")
    except yaml.YAMLError as exc:
        raise PolicyError("authorization policy is invalid YAML") from exc


def _resource_grants(value: object) -> set[ResourceGrant]:
    """Validate exact resource pairs and the D0 device fan-out ceiling."""
    if isinstance(value, (str, bytes)) or not isinstance(value, list) or not value:
        raise PolicyError("allow must be a non-empty sequence")
    resources: set[ResourceGrant] = set()
    folded_pairs: set[tuple[str, str]] = set()
    folded_runs: dict[str, str] = {}
    folded_devices: dict[str, str] = {}
    devices: set[str] = set()
    for index, raw_resource in enumerate(value):
        resource = _mapping(raw_resource, f"allow[{index}]")
        _exact_keys(resource, {"run_id", "device_id"}, f"allow[{index}]")
        run_id = _resource_identifier(resource["run_id"], "run_id")
        device_id = _resource_identifier(resource["device_id"], "device_id")
        grant = ResourceGrant(run_id=run_id, device_id=device_id)
        if grant in resources:
            raise PolicyError("allow contains a duplicate run/device pair")
        folded = (run_id.casefold(), device_id.casefold())
        if folded in folded_pairs:
            raise PolicyError("allow contains a case-ambiguous run/device pair")
        previous_run = folded_runs.get(folded[0])
        if previous_run is not None and previous_run != run_id:
            raise PolicyError("allow contains a case-ambiguous run_id")
        previous_device = folded_devices.get(folded[1])
        if previous_device is not None and previous_device != device_id:
            raise PolicyError("allow contains a case-ambiguous device_id")
        folded_pairs.add(folded)
        folded_runs[folded[0]] = run_id
        folded_devices[folded[1]] = device_id
        resources.add(grant)
        devices.add(device_id)
    if len(devices) > MAX_AUTHORIZED_DEVICES:
        raise PolicyError("a D0 subject may access at most 10 devices")
    return resources


@dataclass(frozen=True)
class TokenVerifier:
    """Verify fixed-format access tokens against an injected runtime key."""

    key: bytes
    issuer: str
    audience: str
    clock: Callable[[], float] = time.time
    max_ttl_seconds: int = MAX_TOKEN_TTL_SECONDS
    clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS

    def __post_init__(self) -> None:
        _secret_key(self.key, "bearer signing key")
        _opaque_identifier(self.issuer, "issuer")
        _opaque_identifier(self.audience, "audience")
        if (
            isinstance(self.max_ttl_seconds, bool)
            or not isinstance(self.max_ttl_seconds, int)
            or self.max_ttl_seconds <= 0
            or self.max_ttl_seconds > MAX_TOKEN_TTL_SECONDS
        ):
            raise ValueError("max_ttl_seconds must be in [1, 900]")
        if (
            isinstance(self.clock_skew_seconds, bool)
            or not isinstance(self.clock_skew_seconds, int)
            or not 0 <= self.clock_skew_seconds <= 30
        ):
            raise ValueError("clock_skew_seconds must be in [0, 30]")

    def verify(self, token: str) -> TokenClaims:
        """Authenticate a canonical token, checking the MAC before claims."""
        if not isinstance(token, str):
            raise TokenError("invalid bearer token")
        try:
            raw = token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise TokenError("invalid bearer token") from exc
        if not raw or len(raw) > MAX_TOKEN_BYTES:
            raise TokenError("invalid bearer token")
        segments = token.split(".")
        if len(segments) != 3 or segments[0] != TOKEN_PREFIX:
            raise TokenError("invalid bearer token")
        payload_segment, signature_segment = segments[1], segments[2]
        signature = _decode_base64url(signature_segment)
        if len(signature) != hashlib.sha256().digest_size:
            raise TokenError("invalid bearer token")
        signing_input = f"{TOKEN_PREFIX}.{payload_segment}".encode("ascii")
        expected = hmac.new(self.key, signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise TokenError("invalid bearer token")

        payload_bytes = _decode_base64url(payload_segment)
        payload = _strict_json_object(payload_bytes)
        try:
            canonical_payload = _canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise TokenError("invalid bearer token") from exc
        if _encode_base64url(canonical_payload) != payload_segment:
            raise TokenError("invalid bearer token")
        if set(payload) != _CLAIM_FIELDS:
            raise TokenError("invalid bearer token")
        if payload["typ"] != TOKEN_TYPE or payload["ver"] != TOKEN_VERSION:
            raise TokenError("invalid bearer token")
        issuer = _claim_identifier(payload["iss"])
        audience = _claim_identifier(payload["aud"])
        subject = _claim_identifier(payload["sub"])
        token_id = _claim_identifier(payload["jti"])
        if len(token_id) < 22:
            raise TokenError("invalid bearer token")
        if issuer != self.issuer or audience != self.audience:
            raise TokenError("invalid bearer token")
        issued_at = _claim_time(payload["iat"])
        expires_at = _claim_time(payload["exp"])
        if expires_at <= issued_at:
            raise TokenError("invalid bearer token")
        if expires_at - issued_at > self.max_ttl_seconds:
            raise TokenError("invalid bearer token")
        now = int(self.clock())
        if issued_at > now + self.clock_skew_seconds:
            raise TokenError("invalid bearer token")
        if now > expires_at + self.clock_skew_seconds:
            raise TokenError("invalid bearer token")
        return TokenClaims(
            issuer=issuer,
            audience=audience,
            subject=subject,
            issued_at=issued_at,
            expires_at=expires_at,
            token_id=token_id,
        )


def issue_token(
    *,
    key: bytes,
    issuer: str,
    audience: str,
    subject: str,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
) -> str:
    """Issue a short-lived token for a previously authenticated subject.

    This helper performs cryptographic formatting only. It is intentionally
    not exposed through HTTP and does not accept or validate a password.
    """
    _secret_key(key, "bearer signing key")
    issuer = _opaque_identifier(issuer, "issuer")
    audience = _opaque_identifier(audience, "audience")
    subject = _opaque_identifier(subject, "subject")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("ttl_seconds must be an integer")
    if not 1 <= ttl_seconds <= MAX_TOKEN_TTL_SECONDS:
        raise ValueError("ttl_seconds must be in [1, 900]")
    issued_at = int(time.time()) if now is None else _plain_int(now, "now")
    actual_token_id = _opaque_identifier(f"jti-{secrets.token_urlsafe(18)}", "token_id")
    claims: dict[str, object] = {
        "aud": audience,
        "exp": issued_at + ttl_seconds,
        "iat": issued_at,
        "iss": issuer,
        "jti": actual_token_id,
        "sub": subject,
        "typ": TOKEN_TYPE,
        "ver": TOKEN_VERSION,
    }
    payload_segment = _encode_base64url(_canonical_json(claims))
    signing_input = f"{TOKEN_PREFIX}.{payload_segment}"
    signature = hmac.new(key, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_encode_base64url(signature)}"


def _canonical_json(payload: Mapping[str, object]) -> bytes:
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
                raise TokenError("invalid bearer token")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise TokenError("invalid bearer token") from exc
    if not isinstance(value, dict):
        raise TokenError("invalid bearer token")
    return value


def _encode_base64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_base64url(segment: str) -> bytes:
    if not segment or "=" in segment or _BASE64URL.fullmatch(segment) is None:
        raise TokenError("invalid bearer token")
    padding = "=" * (-len(segment) % 4)
    try:
        payload = base64.b64decode(
            (segment + padding).encode("ascii"), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise TokenError("invalid bearer token") from exc
    if _encode_base64url(payload) != segment:
        raise TokenError("invalid bearer token")
    return payload


def _claim_identifier(value: object) -> str:
    try:
        return _opaque_identifier(value, "claim")
    except (PolicyError, ValueError) as exc:
        raise TokenError("invalid bearer token") from exc


def _claim_time(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TokenError("invalid bearer token")
    return value


def _secret_key(value: object, label: str) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError(f"{label} must be at least 32 runtime-injected bytes")
    return value


def _plain_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _opaque_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PolicyError(f"{label} must be a bounded ASCII identifier")
    if "*" in value:
        raise PolicyError(f"{label} must not contain a wildcard")
    return value


def _resource_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _RESOURCE_IDENTIFIER.fullmatch(value) is None:
        raise PolicyError(f"{label} must be a safe bounded ASCII identifier")
    if value in {".", ".."} or "*" in value:
        raise PolicyError(f"{label} must not be a path or wildcard")
    return value


def _mapping(value: object, label: str) -> dict[Any, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{label} must be a mapping")
    return value


def _exact_keys(value: Mapping[Any, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise PolicyError(f"{label} has missing or unknown fields")
