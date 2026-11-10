"""Static YAML authorisation snapshot for the D0 query subset.

Implements ADR-0007 section 5.1.3: a startup-loaded policy file mapping
subjects to the runs and devices they may read. Loading is deliberately
strict. Unknown keys, duplicate keys, YAML tags, merge keys, aliases,
wildcards, duplicate allow-pairs, case-folding subject-id collisions and
non-string values are all rejected rather than silently ignored.

The policy grants nothing implicitly. After the bearer authenticates a
subject, the application asks this module whether that subject may read a
specific run_id + device_id pair; any miss is surfaced as 404 so the service
never discloses resource existence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml
from yaml import events, nodes

from g15.query.errors import AuthorizationError

AUTHZ_SCHEMA_VERSION = "1"
MAX_DEVICES_PER_SUBJECT = 10
MAX_ALLOW_PAIRS_PER_SUBJECT = 64

_TAG_NULL = "tag:yaml.org,2002:null"
_TAG_STR = "tag:yaml.org,2002:str"

_ROOT_KEYS = frozenset({"authz_schema_version", "policy_version", "subjects"})
_SUBJECT_KEYS = frozenset({"actor_ref", "permission_domain", "allow"})
_ALLOW_KEYS = frozenset({"run_id", "device_id"})


@dataclass(frozen=True)
class AllowPair:
    run_id: str
    device_id: str


@dataclass(frozen=True)
class Subject:
    actor_ref: str
    permission_domain: str
    allow: tuple[AllowPair, ...]

    def allows(self, run_id: str, device_id: str) -> bool:
        pair = AllowPair(run_id=run_id, device_id=device_id)
        return pair in self.allow


@dataclass(frozen=True)
class AuthzPolicy:
    authz_schema_version: str
    policy_version: str
    subjects: tuple[tuple[str, Subject], ...]

    _by_id: dict[str, Subject] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", dict(self.subjects))

    def subject(self, subject_id: str) -> Subject | None:
        return self._by_id.get(subject_id)


def _scalar(node):
    if node.tag == _TAG_NULL:
        return None
    if node.tag == _TAG_STR:
        return node.value
    raise AuthorizationError("authorisation file values must be quoted strings")


def _construct(node):
    if isinstance(node, nodes.ScalarNode):
        return _scalar(node)
    if isinstance(node, nodes.SequenceNode):
        return [_construct(child) for child in node.value]
    if isinstance(node, nodes.MappingNode):
        result = {}
        for key_node, value_node in node.value:
            key = _construct(key_node)
            if key in result:
                raise AuthorizationError("authorisation file contains a duplicate key")
            result[key] = _construct(value_node)
        return result
    raise AuthorizationError("authorisation file contains an unsupported node")


def _require_dict(value, label):
    if not isinstance(value, dict):
        raise AuthorizationError("%s must be a mapping" % label)
    return value


def _require_string(value, label):
    if not isinstance(value, str) or not value:
        raise AuthorizationError("%s must be a non-empty string" % label)
    return value


def _require_identifier(value, label):
    value = _require_string(value, label)
    if "*" in value or "?" in value:
        raise AuthorizationError("%s must not contain wildcard characters" % label)
    return value


def _require_key_set(value, expected, label):
    _require_dict(value, label)
    keys = set(value)
    if keys != expected:
        raise AuthorizationError("%s has unexpected or missing keys" % label)
    return value


class _NoAliasLoader(yaml.SafeLoader):
    """A SafeLoader that rejects YAML aliases before they are resolved."""

    def compose_node(self, parent, index):
        if self.check_event(events.AliasEvent):
            raise AuthorizationError("authorisation file must not use YAML aliases")
        return super().compose_node(parent, index)


def load_authz(text: str) -> AuthzPolicy:
    """Parse and validate an authorisation policy document.

    Raises:
        AuthorizationError: for any structural or semantic violation. The
            message is internal only; the transport surfaces a uniform 404.
    """
    if not isinstance(text, str) or not text.strip():
        raise AuthorizationError("authorisation file is empty")
    try:
        root_node = yaml.compose(text, Loader=_NoAliasLoader)
    except yaml.YAMLError as exc:
        raise AuthorizationError("authorisation file is not valid YAML") from exc
    if root_node is None:
        raise AuthorizationError("authorisation file is empty")
    root = _require_dict(_construct(root_node), "authorisation root")
    _require_key_set(root, _ROOT_KEYS, "authorisation root")

    if root["authz_schema_version"] != AUTHZ_SCHEMA_VERSION:
        raise AuthorizationError("unsupported authz_schema_version")
    policy_version = _require_string(root["policy_version"], "policy_version")

    subjects_raw = _require_dict(root["subjects"], "subjects")
    if not subjects_raw:
        raise AuthorizationError("subjects must not be empty")

    folded: dict[str, str] = {}
    subjects: list[tuple[str, Subject]] = []
    for subject_id, subject_raw in subjects_raw.items():
        subject_id = _require_identifier(subject_id, "subject id")
        folded_key = subject_id.casefold()
        if folded_key in folded:
            raise AuthorizationError("subject ids collide under case folding")
        folded[folded_key] = subject_id
        _require_key_set(subject_raw, _SUBJECT_KEYS, "subject")
        actor_ref = _require_identifier(subject_raw["actor_ref"], "actor_ref")
        permission_domain = _require_string(
            subject_raw["permission_domain"], "permission_domain"
        )
        allow_raw = subject_raw["allow"]
        if not isinstance(allow_raw, list) or not allow_raw:
            raise AuthorizationError("subject allow must be a non-empty list")
        if len(allow_raw) > MAX_ALLOW_PAIRS_PER_SUBJECT:
            raise AuthorizationError("subject allow list exceeds the cap")
        seen: set[tuple[str, str]] = set()
        devices: set[str] = set()
        pairs: list[AllowPair] = []
        for entry in allow_raw:
            _require_key_set(entry, _ALLOW_KEYS, "allow entry")
            run_id = _require_identifier(entry["run_id"], "run_id")
            device_id = _require_identifier(entry["device_id"], "device_id")
            key = (run_id, device_id)
            if key in seen:
                raise AuthorizationError("duplicate allow pair for subject")
            seen.add(key)
            devices.add(device_id)
            pairs.append(AllowPair(run_id=run_id, device_id=device_id))
        if len(devices) > MAX_DEVICES_PER_SUBJECT:
            raise AuthorizationError("subject exceeds the device fan-out cap")
        subjects.append(
            (
                subject_id,
                Subject(
                    actor_ref=actor_ref,
                    permission_domain=permission_domain,
                    allow=tuple(pairs),
                ),
            )
        )

    return AuthzPolicy(
        authz_schema_version=AUTHZ_SCHEMA_VERSION,
        policy_version=policy_version,
        subjects=tuple(subjects),
    )
