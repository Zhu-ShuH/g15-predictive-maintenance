"""Guard the reviewed acceptance scenario list against the committed configs.

``docs/acceptance.md`` section 3 fixes which profiles are judged without an
injected event, and ``g15.reporting.builder.ACCEPTANCE_SCENARIO_KINDS`` is the
only place that list exists in code.  The two can drift silently in both
directions: a new acceptance profile would fall to ``passed=null`` with nobody
noticing, and a deleted profile would leave a dead key that still claims a
verdict.  This test walks ``configs/acceptance_*.yaml`` and checks both
directions, so adding or removing a profile fails here instead of shipping a
quietly wrong report (review of Issue #9, R8).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from g15.config import load_config
from g15.reporting.builder import ACCEPTANCE_SCENARIO_KINDS
from g15.reporting.metrics import SCENARIO_DEGRADATION, SCENARIO_NORMAL

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPOSITORY_ROOT / "configs"
ACCEPTANCE_CONFIGS = sorted(CONFIGS.glob("acceptance_*.yaml"))


def _profile(scenario_id: str) -> tuple[bool, str]:
    """Return ``(injects_events, family)`` for one acceptance profile id.

    Args:
        scenario_id: The ``scenario.id`` of a committed acceptance profile.

    Returns:
        Whether the profile schedules at least one fault event, and the profile
        family named by the file (the part between ``acceptance_`` and the
        seed suffix).
    """
    config = load_config(CONFIGS / f"{scenario_id}.yaml")
    family = scenario_id.removeprefix("acceptance_").rsplit("_seed", 1)[0]
    return (bool(config.fault_generation.events), family)


def test_every_acceptance_config_is_readable() -> None:
    """The fixture must find the committed profiles, not an empty directory."""
    assert len(ACCEPTANCE_CONFIGS) == 10


def test_every_event_free_profile_is_listed() -> None:
    """A profile without events must be on the list, or it loses its verdict.

    A profile that injects nothing cannot be classified from the run directory,
    so leaving it off the list would silently demote a reviewed acceptance run
    to ``passed=null``.
    """
    missing = []
    for path in ACCEPTANCE_CONFIGS:
        config = load_config(path)
        scenario_id = config.scenario.scenario_id
        injects_events, _ = _profile(scenario_id)
        if not injects_events and scenario_id not in ACCEPTANCE_SCENARIO_KINDS:
            missing.append(scenario_id)
    assert missing == [], (
        "these event-free acceptance profiles are missing from "
        f"ACCEPTANCE_SCENARIO_KINDS: {missing}"
    )


def test_every_listed_id_still_exists() -> None:
    """A key without a committed profile is dead and must be removed."""
    committed = {load_config(path).scenario.scenario_id for path in ACCEPTANCE_CONFIGS}
    dead = sorted(set(ACCEPTANCE_SCENARIO_KINDS) - committed)
    assert dead == [], f"these listed ids have no committed profile: {dead}"


def test_no_profile_that_injects_events_is_listed() -> None:
    """Listing an event profile would hide the evidence branch if it broke.

    The six spike/step/burst profiles must be classified by the truth file, not
    by a name lookup, so a key for one of them would mask a broken
    ``has_events`` branch.
    """
    listed = [
        scenario_id
        for scenario_id in ACCEPTANCE_SCENARIO_KINDS
        if _profile(scenario_id)[0]
    ]
    assert listed == [], (
        "these profiles inject events and must not be listed: " f"{listed}"
    )


@pytest.mark.parametrize(
    ("scenario_id", "expected"),
    [
        ("acceptance_healthy_seed42", SCENARIO_NORMAL),
        ("acceptance_healthy_seed20261016", SCENARIO_NORMAL),
        ("acceptance_degradation_seed42", SCENARIO_DEGRADATION),
        ("acceptance_degradation_seed20261016", SCENARIO_DEGRADATION),
    ],
)
def test_each_listed_kind_matches_its_profile_family(
    scenario_id: str, expected: str
) -> None:
    """The listed kind must be the one the profile's family describes."""
    _, family = _profile(scenario_id)
    assert family in {"healthy", "degradation"}
    assert ACCEPTANCE_SCENARIO_KINDS[scenario_id] == expected
    if family == "healthy":
        assert ACCEPTANCE_SCENARIO_KINDS[scenario_id] == SCENARIO_NORMAL
    else:
        assert ACCEPTANCE_SCENARIO_KINDS[scenario_id] == SCENARIO_DEGRADATION
