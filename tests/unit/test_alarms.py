"""Unit tests for alarm evaluation (Issue #7, owner B2).

The state machine is checked window by window: debounce (N consecutive
breaches), escalation, no downgrade, hysteresis on recovery, and the rejection of
windows that arrive out of order or without a position. The scores are produced
by the real scoring path instead of a fake, so the tests also cover the joint
behaviour of scoring and alarms.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from g15.analysis.alarms import (
    LEVEL_RANK,
    Alarm,
    AlarmState,
    classify_level,
    evaluate_alarm,
)
from g15.analysis.features import FeatureRecord
from g15.analysis.health import (
    HealthResult,
    HealthRules,
    parse_health_rules,
    score_health,
)

WINDOW = 256


def make_rules(
    *,
    scoring_version: str = "test-v1",
    rule_id: str = "test_rule",
    **thresholds: int | float,
) -> HealthRules:
    """Return a single-feature rule set whose score maps linearly onto RMS."""
    document = {
        "schema_version": "1.0",
        "scoring_version": scoring_version,
        "rule_id": rule_id,
        "thresholds": {
            "yellow": 70.0,
            "red": 40.0,
            "consecutive_windows": 1,
            "recovery_windows": 1,
        },
        "features": [
            {
                "name": "rms",
                "baseline": 0.5,
                "tolerance": 0.5,
                "weight": 1.0,
                "direction": "increase",
            }
        ],
    }
    document["thresholds"].update(thresholds)
    return parse_health_rules(document)


def health_at(rules: HealthRules, start_seq: int, score: float) -> HealthResult:
    """Return a health result whose score is ``score`` by inverting the rule.

    ``health = 100 * (1 - risk)`` and ``risk = (rms - 0.5) / 0.5``, so
    ``rms = 0.5 + 0.5 * (1 - score / 100)``.
    """
    rms = 0.5 + 0.5 * (1.0 - score / 100.0)
    features = FeatureRecord(
        mean=0.5,
        std=0.05,
        rms=rms,
        ptp=0.28,
        kurtosis_excess=0.0,
        crest_factor=1.27,
        dominant_freq_hz=25.0,
        zero_crossing_rate=0.5,
        sample_count=WINDOW,
        sample_rate_hz=100,
        window_start_seq=start_seq,
    )
    return score_health(features, rules)


def feed(
    rules: HealthRules, state: AlarmState, samples: list[tuple[int, float]]
) -> tuple[list[Alarm], AlarmState]:
    """Push a list of ``(start_seq, score)`` windows through the machine."""
    alarms: list[Alarm] = []
    for start_seq, score in samples:
        new_alarms, state = evaluate_alarm(
            health_at(rules, start_seq, score), state, rules
        )
        alarms.extend(new_alarms)
    return alarms, state


def test_healthy_window_raises_nothing_and_keeps_state_clean() -> None:
    """A score above the yellow threshold is not an alarm and resets the streak."""
    rules = make_rules()
    alarms, state = feed(rules, AlarmState(), [(0, 100.0)])

    assert alarms == []
    assert state.active_level is None
    assert state.consecutive_breaches == 0
    assert state.last_window_start_seq == 0


def test_yellow_alarm_is_raised_immediately_when_n_is_one() -> None:
    """With ``consecutive_windows: 1`` the first breaching window alarms."""
    rules = make_rules()
    alarms, state = feed(rules, AlarmState(), [(WINDOW, 60.0)])

    assert len(alarms) == 1
    alarm = alarms[0]
    assert alarm.level == "yellow"
    assert alarm.start_seq == WINDOW
    assert alarm.health_score == pytest.approx(60.0)
    assert alarm.rule_id == "test_rule"
    assert alarm.scoring_version == "test-v1"
    assert "yellow threshold 70.00" in alarm.reason
    assert alarm.drivers == ("rms",)
    assert state.active_level == "yellow"
    assert state.alarm_windows == 1


def test_red_alarm_is_raised_when_score_is_at_or_below_red() -> None:
    """The boundary belongs to the severe level: 40.0 is red, not yellow."""
    rules = make_rules()
    alarms, _ = feed(rules, AlarmState(), [(0, 40.0)])

    assert len(alarms) == 1
    assert alarms[0].level == "red"
    assert "red threshold 40.00" in alarms[0].reason


def test_debounce_requires_n_consecutive_breaches() -> None:
    """With N=3 the first two bad windows stay silent; the third alarms once."""
    rules = make_rules(consecutive_windows=3)
    alarms, state = feed(
        rules,
        AlarmState(),
        [(0, 60.0), (WINDOW, 60.0), (2 * WINDOW, 60.0), (3 * WINDOW, 60.0)],
    )

    assert [alarm.start_seq for alarm in alarms] == [2 * WINDOW]
    assert state.active_level == "yellow"
    assert state.consecutive_breaches == 4
    assert state.alarm_windows == 1


def test_a_single_healthy_window_resets_the_debounce_counter() -> None:
    """Two breaches, a healthy window, two breaches: still no alarm with N=3."""
    rules = make_rules(consecutive_windows=3)
    alarms, state = feed(
        rules,
        AlarmState(),
        [
            (0, 60.0),
            (WINDOW, 60.0),
            (2 * WINDOW, 100.0),
            (3 * WINDOW, 60.0),
            (4 * WINDOW, 60.0),
        ],
    )

    assert alarms == []
    assert state.active_level is None
    assert state.consecutive_breaches == 2


def test_sustained_breach_does_not_repeat_the_alarm() -> None:
    """Edge triggering: ten red windows produce one red alarm, not ten."""
    rules = make_rules()
    windows = [(index * WINDOW, 10.0) for index in range(10)]
    alarms, state = feed(rules, AlarmState(), windows)

    assert len(alarms) == 1
    assert alarms[0].start_seq == 0
    assert state.alarm_windows == 1


def test_escalation_from_yellow_to_red_raises_a_second_alarm() -> None:
    """A worsening score must upgrade the level exactly once."""
    rules = make_rules(consecutive_windows=2)
    alarms, state = feed(
        rules,
        AlarmState(),
        [(0, 60.0), (WINDOW, 60.0), (2 * WINDOW, 20.0), (3 * WINDOW, 20.0)],
    )

    assert [alarm.level for alarm in alarms] == ["yellow", "red"]
    assert [alarm.start_seq for alarm in alarms] == [WINDOW, 2 * WINDOW]
    assert state.active_level == "red"
    assert state.alarm_windows == 2


def test_level_never_downgrades_while_the_alarm_is_latched() -> None:
    """Red then yellow must not emit a milder alarm; severity stays red."""
    rules = make_rules()
    alarms, state = feed(rules, AlarmState(), [(0, 20.0), (WINDOW, 60.0)])

    assert len(alarms) == 1
    assert alarms[0].level == "red"
    assert state.active_level == "red"


def test_recovery_needs_m_consecutive_clean_windows() -> None:
    """With M=2 the first clean window keeps the alarm latched, the second clears it."""
    rules = make_rules(recovery_windows=2)
    state = AlarmState()
    _, state = evaluate_alarm(health_at(rules, 0, 20.0), state, rules)
    assert state.active_level == "red"

    _, state = evaluate_alarm(health_at(rules, WINDOW, 100.0), state, rules)
    assert state.active_level == "red"
    assert state.recovered_at_seq is None

    _, state = evaluate_alarm(health_at(rules, 2 * WINDOW, 100.0), state, rules)
    assert state.active_level is None
    assert state.recovered_at_seq == 2 * WINDOW
    assert state.normal_windows == 2
    assert state.alarm_windows == 0


def test_alarm_can_be_raised_again_after_recovery() -> None:
    """A second episode must produce a new alarm rather than stay silent."""
    rules = make_rules()
    alarms, state = feed(
        rules,
        AlarmState(),
        [(0, 20.0), (WINDOW, 100.0), (2 * WINDOW, 20.0)],
    )

    assert [alarm.start_seq for alarm in alarms] == [0, 2 * WINDOW]
    assert state.active_level == "red"
    assert state.alarm_windows == 1


def test_state_survives_a_round_trip_through_plain_data() -> None:
    """C2 stores the state as columns; rebuilding it must not change behaviour."""
    rules = make_rules(consecutive_windows=2)
    _, state = feed(rules, AlarmState(), [(0, 60.0), (WINDOW, 60.0)])

    rebuilt = AlarmState(**dataclasses.asdict(state))
    assert rebuilt == state

    alarms_a, state_a = feed(rules, state, [(2 * WINDOW, 20.0)])
    alarms_b, state_b = feed(rules, rebuilt, [(2 * WINDOW, 20.0)])
    assert alarms_a == alarms_b
    assert state_a == state_b


def test_missing_window_position_is_rejected() -> None:
    """An alarm without a window cannot be matched, so it must not be invented."""
    rules = make_rules()
    features = FeatureRecord(
        mean=0.5,
        std=0.05,
        rms=0.9,
        ptp=0.28,
        kurtosis_excess=0.0,
        crest_factor=1.27,
        dominant_freq_hz=25.0,
        zero_crossing_rate=0.5,
        sample_count=WINDOW,
        sample_rate_hz=100,
        window_start_seq=None,
    )
    with pytest.raises(ValueError, match="window_start_seq is required"):
        evaluate_alarm(score_health(features, rules), AlarmState(), rules)


def test_out_of_order_window_is_rejected() -> None:
    """Replaying an old window would double count a breach."""
    rules = make_rules()
    state = AlarmState(last_window_start_seq=10 * WINDOW)
    with pytest.raises(ValueError, match="must increase"):
        evaluate_alarm(health_at(rules, 5 * WINDOW, 60.0), state, rules)


def test_repeated_window_position_is_rejected() -> None:
    """Idempotent replay is the consumer's job; the state machine refuses it."""
    rules = make_rules()
    state = AlarmState(last_window_start_seq=WINDOW)
    with pytest.raises(ValueError, match="must increase"):
        evaluate_alarm(health_at(rules, WINDOW, 60.0), state, rules)


def test_result_from_another_scoring_version_is_rejected() -> None:
    """A stale result must not be classified with newer thresholds."""
    rules = make_rules()
    health = health_at(rules, 0, 60.0)
    stale = dataclasses.replace(health, scoring_version="heuristic-v0")
    with pytest.raises(ValueError, match="scoring_version"):
        evaluate_alarm(stale, AlarmState(), rules)


def test_result_from_another_rule_set_is_rejected() -> None:
    """Mixing rule ids would break the dedupe key."""
    rules = make_rules()
    health = health_at(rules, 0, 60.0)
    other = dataclasses.replace(health, rule_id="another_rule")
    with pytest.raises(ValueError, match="rule_id"):
        evaluate_alarm(other, AlarmState(), rules)


def test_non_finite_score_is_rejected() -> None:
    """A NaN score has no level; guessing one would hide a bug."""
    rules = make_rules()
    health = health_at(rules, 0, 60.0)
    broken = dataclasses.replace(health, health_score=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        evaluate_alarm(broken, AlarmState(), rules)


def test_levels_are_monotonic_in_the_score() -> None:
    """A lower score can never be a milder alarm (no inverted threshold)."""
    rules = make_rules()
    levels = [classify_level(score, rules) for score in range(100, -1, -1)]
    ranks = [0 if level is None else LEVEL_RANK[level] for level in levels]

    assert ranks == sorted(ranks)


def test_dedupe_key_is_device_rule_and_window() -> None:
    """Section 3 of the acceptance spec keys alarms on three columns."""
    rules = make_rules()
    alarms, _ = feed(rules, AlarmState(), [(4096, 20.0)])
    alarm = alarms[0]

    assert alarm.dedupe_key("DEV-001") == ("DEV-001", "test_rule", 4096)
    assert alarm.dedupe_key("DEV-001") != alarm.dedupe_key("DEV-002")


def test_alarm_is_immutable() -> None:
    """A raised alarm is a record of the past and must not be edited."""
    rules = make_rules()
    alarms, _ = feed(rules, AlarmState(), [(0, 20.0)])
    with pytest.raises(AttributeError):
        alarms[0].level = "yellow"  # type: ignore[misc]


def make_retuned_rules() -> HealthRules:
    """Return the same features with a new identity and a 3-window debounce.

    This is the shape of a rule retune: ``health_rules.yaml`` is edited, the
    version is bumped, and no Python changes. A state persisted under the old
    version must not be allowed to satisfy the new debounce.
    """
    return make_rules(
        scoring_version="test-v2",
        rule_id="test_rule_v2",
        yellow=80.0,
        red=60.0,
        consecutive_windows=3,
    )


def test_state_records_the_rule_identity_that_produced_it() -> None:
    """A state leaves the machine tagged, so a later run can detect a retune."""
    rules = make_rules()
    _, state = feed(rules, AlarmState(), [(0, 60.0)])

    assert state.scoring_version == "test-v1"
    assert state.rule_id == "test_rule"


def test_state_from_another_version_is_rejected_when_it_carries_a_streak() -> None:
    """Two old breaches must not count towards a new rule's debounce.

    Without the identity check the three-window debounce of ``test-v2`` is
    satisfied by a single breaching window, because the two breaches counted
    under ``test-v1`` thresholds are silently inherited.
    """
    rules = make_retuned_rules()
    carried = AlarmState(
        consecutive_breaches=2,
        last_window_start_seq=0,
        scoring_version="test-v1",
        rule_id="test_rule",
    )

    with pytest.raises(ValueError, match="scoring_version"):
        evaluate_alarm(health_at(rules, WINDOW, 50.0), carried, rules)


def test_latched_state_from_another_version_is_rejected() -> None:
    """A latched alarm must be reset deliberately, not carried over."""
    rules = make_retuned_rules()
    carried = AlarmState(
        active_level="yellow",
        last_window_start_seq=0,
        scoring_version="test-v1",
        rule_id="test_rule",
    )

    with pytest.raises(ValueError, match="active_level='yellow'"):
        evaluate_alarm(health_at(rules, WINDOW, 50.0), carried, rules)


def test_state_with_a_different_rule_id_is_rejected() -> None:
    """The rule id is checked as well, not just the version string."""
    rules = make_retuned_rules()
    carried = AlarmState(
        consecutive_breaches=1,
        last_window_start_seq=0,
        scoring_version="test-v2",
        rule_id="test_rule",
    )

    with pytest.raises(ValueError, match="rule_id"):
        evaluate_alarm(health_at(rules, WINDOW, 50.0), carried, rules)


def test_state_without_history_adopts_the_current_rule_identity() -> None:
    """A stale identity on a state with nothing to inherit is harmless.

    A healthy window written before the retune leaves no streak and no alarm, so
    rejecting it would block consumption for no reason; the state is re-tagged
    with the current identity instead.
    """
    rules = make_retuned_rules()
    carried = AlarmState(
        last_window_start_seq=0,
        scoring_version="test-v1",
        rule_id="test_rule",
    )

    alarms, state = evaluate_alarm(health_at(rules, WINDOW, 50.0), carried, rules)

    assert alarms == []
    assert state.consecutive_breaches == 1
    assert state.scoring_version == "test-v2"
    assert state.rule_id == "test_rule_v2"


def test_state_without_a_recorded_identity_stays_backward_compatible() -> None:
    """``AlarmState()`` still starts a streak, as it did before the field existed."""
    rules = make_rules()
    alarms, state = feed(rules, AlarmState(), [(0, 60.0)])

    assert len(alarms) == 1
    assert state.scoring_version == "test-v1"


def _json_round_trip(state: AlarmState, *, drop: tuple[str, ...] = ()) -> AlarmState:
    """Serialise a state the way a ``state_json`` column would and read it back.

    ``drop`` removes fields to reproduce a row written before the identity
    columns existed, which is the case a six-field restore hits.
    """
    payload = {
        key: value
        for key, value in dataclasses.asdict(state).items()
        if key not in drop
    }
    return AlarmState(**json.loads(json.dumps(payload)))


def _streak_under_old_rules(count: int = 2) -> AlarmState:
    """Return a state carrying ``count`` breaches scored by ``test-v1``."""
    rules = make_rules(consecutive_windows=3)
    _, state = feed(rules, AlarmState(), [(0, 50.0), (256, 50.0)][:count])
    assert state.consecutive_breaches == count
    return state


def test_six_field_json_restore_with_a_streak_is_rejected() -> None:
    """A legacy row that lost the identity fields is not evidence of a fresh start.

    This is the case a pre-``state_json``-v1 row hits: the six state fields are
    restored, both identity fields default to ``None``, and treating that as
    fresh would let the restored counters satisfy the debounce of whatever rules
    are loaded now.
    """
    carried = _json_round_trip(
        _streak_under_old_rules(), drop=("scoring_version", "rule_id")
    )
    rules = make_retuned_rules()

    assert carried.scoring_version is None
    assert carried.consecutive_breaches == 2
    with pytest.raises(ValueError, match="does not record scoring_version"):
        evaluate_alarm(health_at(rules, 512, 50.0), carried, rules)


def test_partially_identified_restore_with_a_streak_is_rejected() -> None:
    """Losing only the version is enough to make the streak unattributable."""
    carried = _json_round_trip(_streak_under_old_rules(), drop=("scoring_version",))
    rules = make_rules(consecutive_windows=3)

    assert carried.rule_id == "test_rule"
    assert carried.scoring_version is None
    with pytest.raises(ValueError, match="does not record scoring_version"):
        evaluate_alarm(health_at(rules, 512, 50.0), carried, rules)


def test_missing_rule_id_on_a_latched_state_is_rejected() -> None:
    """The check covers ``rule_id`` as well, not just the version."""
    carried = AlarmState(
        active_level="yellow",
        last_window_start_seq=0,
        scoring_version="test-v1",
    )

    with pytest.raises(ValueError, match="does not record rule_id"):
        evaluate_alarm(health_at(make_rules(), WINDOW, 50.0), carried, make_rules())


def test_json_restore_of_a_fully_identified_streak_matches_the_in_memory_result() -> (
    None
):
    """A round trip that keeps both fields must not change the outcome.

    Without this the rejection above could be satisfied by breaking the honest
    restore path too.
    """
    rules = make_retuned_rules()
    carried = _json_round_trip(_streak_under_old_rules())
    carried = dataclasses.replace(
        carried, scoring_version="test-v2", rule_id="test_rule_v2"
    )

    alarms, state = evaluate_alarm(health_at(rules, 512, 50.0), carried, rules)

    assert [a.level for a in alarms] == ["red"]
    assert state.consecutive_breaches == 3
    assert state.scoring_version == "test-v2"


def test_fresh_state_is_the_only_documented_way_to_drop_a_streak() -> None:
    """``AlarmState()`` stays the documented way to drop a streak, nothing else."""
    rules = make_retuned_rules()
    alarms, state = evaluate_alarm(health_at(rules, 512, 50.0), AlarmState(), rules)

    assert alarms == []
    assert state.consecutive_breaches == 1


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (AlarmState(active_level="green"), "active_level"),
        (AlarmState(consecutive_breaches=-1), "consecutive_breaches"),
        (AlarmState(alarm_windows=True), "alarm_windows"),
        (AlarmState(last_window_start_seq="4096"), "last_window_start_seq"),
        (AlarmState(recovered_at_seq=-5), "recovered_at_seq"),
    ],
)
def test_a_restored_state_with_impossible_fields_is_rejected(
    state: AlarmState, message: str
) -> None:
    """Restoration is the one boundary where field values come from outside.

    A bad level string used to reach ``LEVEL_RANK`` as a ``KeyError``, and a
    negative counter would silently lower the debounce, so both must fail with
    the field named instead.
    """
    rules = make_rules()

    with pytest.raises(ValueError, match=message):
        evaluate_alarm(health_at(rules, WINDOW, 50.0), state, rules)


def test_history_without_a_window_position_is_rejected() -> None:
    """Without the last position a replayed window cannot be detected."""
    carried = AlarmState(
        consecutive_breaches=2,
        scoring_version="test-v1",
        rule_id="test_rule",
    )

    with pytest.raises(ValueError, match="last_window_start_seq is None"):
        evaluate_alarm(health_at(make_rules(), WINDOW, 50.0), carried, make_rules())


@pytest.mark.parametrize("bad", [True, 4096.0, "4096"])
def test_window_position_must_be_a_real_integer(bad: object) -> None:
    """``True`` and ``4096.0`` pass loose numeric checks but are not positions."""
    rules = make_rules()
    health = dataclasses.replace(health_at(rules, 0, 50.0), window_start_seq=bad)

    with pytest.raises(ValueError, match="window_start_seq must be an int"):
        evaluate_alarm(health, AlarmState(), rules)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_classify_level_refuses_a_non_finite_score(bad: object) -> None:
    """``NaN`` compares false against both thresholds and would read as healthy."""
    rules = make_rules()

    with pytest.raises(ValueError, match="finite"):
        classify_level(bad, rules)  # type: ignore[arg-type]
