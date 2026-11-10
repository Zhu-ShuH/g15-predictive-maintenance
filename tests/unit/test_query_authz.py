"""Unit tests for the static authorisation snapshot (ADR 5.1.3).

Loading is strict: every structural violation must raise AuthorizationError,
never be silently ignored, because a silently-truncated allow list would leak
read access. These tests assert the exception type; the message stays internal.
"""

from __future__ import annotations

import pytest

from g15.query.authz import (
    MAX_DEVICES_PER_SUBJECT,
    load_authz,
)
from g15.query.errors import AuthorizationError

_BASE = """authz_schema_version: "1"
policy_version: "v1"
subjects:
  s1:
    actor_ref: "s1"
    permission_domain: "d"
    allow:
      - run_id: "r1"
        device_id: "d1"
"""


def test_valid_policy_parses_and_allows():
    policy = load_authz(_BASE)
    assert policy.authz_schema_version == "1"
    assert policy.policy_version == "v1"
    subject = policy.subject("s1")
    assert subject is not None
    assert subject.actor_ref == "s1"
    assert subject.allows("r1", "d1")
    assert not subject.allows("r1", "other")


def test_missing_subject_lookup_is_none():
    policy = load_authz(_BASE)
    assert policy.subject("nobody") is None


def test_empty_text_rejected():
    with pytest.raises(AuthorizationError):
        load_authz("")


def test_invalid_yaml_rejected():
    with pytest.raises(AuthorizationError):
        load_authz("{ not: [valid")


def test_unknown_root_key_rejected():
    with pytest.raises(AuthorizationError):
        load_authz(_BASE + "extra: true\n")


def test_unknown_subject_key_rejected():
    bad = _BASE.replace("permission_domain", "bogus")
    with pytest.raises(AuthorizationError):
        load_authz(bad)


def test_wrong_schema_version_rejected():
    with pytest.raises(AuthorizationError):
        load_authz(
            _BASE.replace('authz_schema_version: "1"', 'authz_schema_version: "2"')
        )


def test_empty_subjects_rejected():
    with pytest.raises(AuthorizationError):
        load_authz(_BASE.replace("subjects:\n  s1:", "subjects: {}"))


def test_wildcard_run_id_rejected():
    bad = _BASE.replace('run_id: "r1"', 'run_id: "r*"')
    with pytest.raises(AuthorizationError):
        load_authz(bad)


def test_duplicate_allow_pair_rejected():
    bad = _BASE + '      - run_id: "r1"\n        device_id: "d1"\n'
    with pytest.raises(AuthorizationError):
        load_authz(bad)


def test_casefold_collision_rejected():
    bad = _BASE.replace(
        "  s1:",
        '  s1:\n    actor_ref: "s1"\n    permission_domain: "d"\n    allow:\n      - run_id: "r1"\n        device_id: "d1"\n  S1:',
    )
    with pytest.raises(AuthorizationError):
        load_authz(bad)


def test_non_string_value_rejected():
    bad = _BASE.replace('actor_ref: "s1"', "actor_ref: 123")
    with pytest.raises(AuthorizationError):
        load_authz(bad)


def test_empty_allow_rejected():
    bad = _BASE.replace('      - run_id: "r1"\n        device_id: "d1"', "      []")
    with pytest.raises(AuthorizationError):
        load_authz(bad)


def test_yaml_alias_rejected():
    bad = _BASE + "  s2:\n    actor_ref: *s1\n"
    with pytest.raises(AuthorizationError):
        load_authz(bad)


def test_device_fanout_cap_rejected():
    entries = "\n".join(
        f'      - run_id: "r1"\n        device_id: "dev{i}"'
        for i in range(MAX_DEVICES_PER_SUBJECT + 1)
    )
    bad = _BASE.replace('      - run_id: "r1"\n        device_id: "d1"', entries)
    with pytest.raises(AuthorizationError):
        load_authz(bad)
