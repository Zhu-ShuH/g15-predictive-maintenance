"""Matching-rule tests for the independent validator (Issue #9).

``docs/acceptance.md`` section 3 fixes the matching rules, and a validator that
implements them loosely would pass a failing pipeline.  These tests therefore
pin the rules themselves -- the allowed window, the one-to-one maximum
matching, the tie break, and the three alarm counts -- on hand-built data where
the expected answer can be computed by hand.

The cases live under ``tests/integration`` because Issue #9 grants B1 the
``tests/integration/*`` path only; they need no run directory, so they stay
pure.
"""

from __future__ import annotations

import pytest

from g15.reporting.metrics import (
    SCENARIO_DEGRADATION,
    SCENARIO_FAULT,
    SCENARIO_NORMAL,
    AlarmRecord,
    TruthEvent,
    allowed_alarm_window,
    evaluate_scenario,
    match_alarms,
    median_halves,
)

WINDOW = 256


def event(
    start: int,
    end: int,
    *,
    event_id: str = "F001",
    device: str = "DEV-001",
    fault_type: str = "spike",
) -> TruthEvent:
    """Build one truth event for the matcher tests."""
    return TruthEvent(
        event_id=event_id,
        device_id=device,
        fault_type=fault_type,
        start_seq=start,
        end_seq_exclusive=end,
        amplitude=5.0,
    )


def alarm(start: int, *, device: str = "DEV-001", rule: str = "health_heuristic"):
    """Build one stored alarm for the matcher tests."""
    return AlarmRecord(device_id=device, rule_id=rule, start_seq=start, level="yellow")


# -- the allowed window ----------------------------------------------------


def test_allowed_window_starts_at_the_touching_window() -> None:
    """An event starting mid-window is detected by that window."""
    low, high = allowed_alarm_window(event(16384, 16640))
    assert low == 16384
    assert high == 16384 + 256 + 2 * WINDOW


def test_allowed_window_uses_floor_and_ceil_not_rounding() -> None:
    """Floor on the start and ceiling on the end, both in whole windows."""
    low, high = allowed_alarm_window(event(100, 200))
    assert low == 0
    # ceil(200/256) = 1, so the last touching window is [0, 256); plus 2 windows.
    assert high == 256 + 2 * WINDOW


def test_event_ending_exactly_on_a_window_boundary_does_not_add_a_window() -> None:
    """The end is exclusive, so ``end == k*W`` must not reach window ``k``."""
    low, high = allowed_alarm_window(event(0, 256))
    assert low == 0
    assert high == 256 + 2 * WINDOW


def test_allowed_window_rejects_a_non_positive_width() -> None:
    """A zero window would make the interval meaningless."""
    with pytest.raises(ValueError, match="window_points must be positive"):
        allowed_alarm_window(event(0, 10), window_points=0)


def test_allowed_window_rejects_a_negative_lag() -> None:
    """A negative grace would silently shrink the window."""
    with pytest.raises(ValueError, match="max_detection_lag_windows"):
        allowed_alarm_window(event(0, 10), max_detection_lag_windows=-1)


# -- one-to-one matching ---------------------------------------------------


def test_one_alarm_cannot_detect_two_events() -> None:
    """A single alarm must be assigned to at most one event."""
    result = match_alarms(
        [event(0, 256, event_id="F001"), event(256, 512, event_id="F002")], [alarm(256)]
    )
    assert result.detected_events == 1
    assert result.missed_events == 1
    assert result.redundant_alarm_count == 0


def test_maximum_matching_beats_greedy_when_a_later_event_has_no_alternative() -> None:
    """The matcher must find the maximum, not the first greedy assignment.

    Event A (earlier) could take either alarm; event B can only take the second.
    A greedy matcher that gives the first alarm to A leaves B undetected, so a
    maximum of two detections is the only correct answer.
    """
    events = [event(0, 256, event_id="F001"), event(256, 512, event_id="F002")]
    alarms = [alarm(256), alarm(512)]
    result = match_alarms(events, alarms)

    assert result.detected_events == 2
    assert result.missed_events == 0
    assert result.detection_rate == 1.0


def test_matching_is_deterministic_under_a_reordered_input() -> None:
    """The same events and alarms must always produce the same assignment."""
    events = [event(0, 256, event_id="F002"), event(0, 256, event_id="F001")]
    alarms = [alarm(256), alarm(256, rule="other_rule")]

    first = match_alarms(events, alarms)
    second = match_alarms(list(reversed(events)), list(reversed(alarms)))

    assert [m.event_id for m in first.matches] == ["F001", "F002"]
    assert [m.matched_alarm_rule for m in first.matches] == [
        m.matched_alarm_rule for m in second.matches
    ]


def test_alarm_of_another_device_never_detects() -> None:
    """Section 3 requires the same ``device_id``."""
    result = match_alarms([event(0, 256)], [alarm(0, device="DEV-002")])
    assert result.detected_events == 0
    assert result.false_alarm_count == 1
    assert result.detection_rate == 0.0


def test_alarm_outside_the_grace_window_is_a_false_alarm() -> None:
    """One window past the grace is already a false alarm."""
    low, high = allowed_alarm_window(event(0, 256))
    assert high == 256 + 2 * WINDOW

    inside = match_alarms([event(0, 256)], [alarm(high - WINDOW)])
    assert inside.detected_events == 1
    assert inside.false_alarm_count == 0

    outside = match_alarms([event(0, 256)], [alarm(high)])
    assert outside.detected_events == 0
    assert outside.false_alarm_count == 1


# -- the three alarm counts ------------------------------------------------


def test_redundant_alarm_is_inside_the_window_but_unmatched() -> None:
    """A second alarm in a detected window is redundant, not a false alarm."""
    result = match_alarms([event(0, 256)], [alarm(0), alarm(256)])
    assert result.detected_events == 1
    assert result.redundant_alarm_count == 1
    assert result.false_alarm_count == 0
    assert result.unmatched_alarm_count == 1


def test_unmatched_alarm_count_is_the_sum_of_false_and_redundant() -> None:
    """Section 3 lists the three counts separately but they must reconcile."""
    result = match_alarms(
        [event(0, 256)],
        [alarm(0), alarm(256), alarm(100_000)],
    )
    assert result.redundant_alarm_count == 1
    assert result.false_alarm_count == 1
    assert result.unmatched_alarm_count == (
        result.false_alarm_count + result.redundant_alarm_count
    )


def test_no_event_means_a_null_rate_and_all_alarms_are_false() -> None:
    """A run with no injected event must report ``None``, never ``0.0``."""
    result = match_alarms([], [alarm(0), alarm(256)])
    assert result.detection_rate is None
    assert result.false_alarm_count == 2
    assert result.redundant_alarm_count == 0


def test_missed_event_reports_its_expected_window() -> None:
    """A missed event still carries the window that should have caught it."""
    result = match_alarms([event(16384, 16640)], [])
    match = result.matches[0]
    assert match.detected is False
    assert match.matched_alarm_start is None
    assert match.first_alarm_start is None
    assert match.allowed_start == 16384
    assert match.allowed_end_exclusive == 16640 + 2 * WINDOW
    assert match.lag_samples is None
    assert result.missed_event_ids() == ("F001",)


def test_lag_is_measured_against_the_event_end() -> None:
    """The lag is the matched alarm start minus the event end, in samples."""
    result = match_alarms([event(1000, 1256)], [alarm(1280)])
    assert result.matches[0].lag_samples == 1280 - 1256


# -- scenario verdicts -----------------------------------------------------


def test_normal_scenario_fails_on_a_single_alarm() -> None:
    """A normal run must fail as soon as one alarm is raised."""
    result = match_alarms([], [alarm(0)])
    verdict = evaluate_scenario(SCENARIO_NORMAL, result)
    assert verdict.passed is False
    assert verdict.kind == SCENARIO_NORMAL


def test_degradation_scenario_is_not_failed_by_its_own_alarm() -> None:
    """A degrading run may alarm; the verdict follows the trend only."""
    result = match_alarms([], [alarm(0)])
    scores = [99.0, 98.0, 97.0, 60.0, 50.0, 40.0]
    verdict = evaluate_scenario(SCENARIO_DEGRADATION, result, health_scores=scores)

    assert verdict.passed is True
    assert verdict.alarm_count == 1
    assert verdict.false_alarm_count == 1


def test_degradation_scenario_fails_when_the_trend_is_flat() -> None:
    """A flat health trace is not a degradation, whatever the alarms say."""
    result = match_alarms([], [])
    verdict = evaluate_scenario(
        SCENARIO_DEGRADATION, result, health_scores=[99.0, 99.0, 99.0, 99.0]
    )
    assert verdict.passed is False


def test_fault_scenario_fails_when_one_event_is_missed() -> None:
    """TP must equal the injected event count, so one miss fails the run."""
    result = match_alarms(
        [event(0, 256, event_id="F001"), event(1024, 1280, event_id="F002")], [alarm(0)]
    )
    verdict = evaluate_scenario(SCENARIO_FAULT, result, expected_events=2)
    assert verdict.passed is False
    assert verdict.detection_rate == 0.5


def test_unknown_scenario_kind_is_rejected() -> None:
    """Silently treating an unknown kind as normal would hide a config error."""
    with pytest.raises(ValueError, match="unknown scenario kind"):
        evaluate_scenario("production", match_alarms([], []))


# -- trend helper ----------------------------------------------------------


def test_median_halves_splits_by_position() -> None:
    """The split is positional, not by score value."""
    first, second = median_halves([10.0, 20.0, 30.0, 1.0])
    assert first == 15.0
    assert second == 15.5


def test_median_halves_needs_two_samples() -> None:
    """One window cannot have a trend, so both medians are ``None``."""
    assert median_halves([42.0]) == (None, None)
    assert median_halves([]) == (None, None)
