"""Detection metrics for the independent validator (Issue #9, owner B1/C5).

``docs/interface.md`` section 5.3 keeps ``private_truth/faults.json`` readable
only by the validator; business modules must never depend on it.  This module
is the pure calculation layer of that validator: it reads no file and touches
no database, so the matching rules of ``docs/acceptance.md`` section 3 can be
exercised in isolation.

Matching rules implemented here (``docs/acceptance.md`` section 3):

* the truth interval is half open, ``[start_seq, end_seq_exclusive)``;
* an event is detectable only by an alarm of the same ``device_id``;
* the allowed alarm window is
  ``[floor(start/W) * W, ceil(end/W) * W + lag * W)``, right end open, where
  ``W`` is the analysis window width and ``lag`` is
  ``max_detection_lag_windows`` (two windows in the controlled profile);
* one alarm may detect at most one event and one event may be detected by at
  most one alarm, so the assignment is a maximum bipartite matching; ties are
  broken deterministically by sorting events on ``(start_seq, event_id)`` and
  alarms on ``(start_seq, rule_id, device_id, level)`` before matching;
* ``detection_rate`` is ``None`` -- never ``100%`` -- when a run carries no
  injected event, and ``null`` is what the report must show for that case.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "MAX_DETECTION_LAG_WINDOWS_DEFAULT",
    "SCENARIO_DEGRADATION",
    "SCENARIO_FAULT",
    "SCENARIO_NORMAL",
    "WINDOW_POINTS_DEFAULT",
    "AlarmRecord",
    "EventMatch",
    "MatchResult",
    "ScenarioVerdict",
    "TruthError",
    "TruthEvent",
    "allowed_alarm_window",
    "evaluate_scenario",
    "load_truth_events",
    "match_alarms",
    "median_halves",
]

#: Analysis window width fixed by ``docs/acceptance.md`` section 3.
WINDOW_POINTS_DEFAULT: int = 256

#: Detection lag allowance after the event ends, in windows.
MAX_DETECTION_LAG_WINDOWS_DEFAULT: int = 2

#: Scenario kinds the validator must keep apart (A review of Issue #9).
SCENARIO_NORMAL: str = "normal"
SCENARIO_DEGRADATION: str = "degradation"
SCENARIO_FAULT: str = "fault"


class TruthError(ValueError):
    """Raised when the truth document does not match section 5.3."""


@dataclass(frozen=True)
class TruthEvent:
    """One injected event read from ``private_truth/faults.json``.

    Attributes:
        event_id: Identifier unique inside the run.
        device_id: Device the event belongs to.
        fault_type: One of ``spike``/``step``/``burst``.
        start_seq: First affected absolute sample index.
        end_seq_exclusive: One past the last affected sample.
        amplitude: Peak perturbation in ``g``.
    """

    event_id: str
    device_id: str
    fault_type: str
    start_seq: int
    end_seq_exclusive: int
    amplitude: float


@dataclass(frozen=True)
class AlarmRecord:
    """One stored alarm row, as read from the ``alarms`` table.

    Attributes:
        device_id: Device the alarm belongs to.
        rule_id: Rule set that raised it; part of the dedupe key.
        start_seq: Absolute start of the offending window.
        level: ``yellow`` or ``red``.
    """

    device_id: str
    rule_id: str
    start_seq: int
    level: str


@dataclass(frozen=True)
class EventMatch:
    """The verdict for one injected event.

    Attributes:
        event_id: The event being judged.
        fault_type: Event type, carried through for the report table.
        start_seq: Event start, absolute sample index.
        end_seq_exclusive: Event end, one past the last sample.
        allowed_start: First alarm start that may count as a detection.
        allowed_end_exclusive: One past the last alarm start that may count.
        matched_alarm_start: Start of the alarm matched to this event, or
            ``None`` when the event was missed.
        matched_alarm_rule: Rule id of that alarm, or ``None``.
        first_alarm_start: Earliest alarm start inside the allowed window,
            matched or not, or ``None`` when the window holds no alarm.
    """

    event_id: str
    fault_type: str
    start_seq: int
    end_seq_exclusive: int
    allowed_start: int
    allowed_end_exclusive: int
    matched_alarm_start: int | None
    matched_alarm_rule: str | None
    first_alarm_start: int | None

    @property
    def detected(self) -> bool:
        """Whether an alarm was assigned to this event."""
        return self.matched_alarm_start is not None

    @property
    def lag_samples(self) -> int | None:
        """Matched alarm start minus the event end, or ``None`` when missed.

        A negative value means the alarm fired on a window that starts before
        the event ends, which is normal for events spanning several windows.
        """
        if self.matched_alarm_start is None:
            return None
        return self.matched_alarm_start - self.end_seq_exclusive


@dataclass(frozen=True)
class MatchResult:
    """The full matching outcome for one run.

    Attributes:
        events: Injected events in deterministic order.
        alarms: Stored alarms in deterministic order.
        matches: One :class:`EventMatch` per event, in the same order.
        false_alarm_count: Alarms whose start falls outside every allowed
            window; in a normal scenario every alarm is a false alarm.
        redundant_alarm_count: Alarms inside some allowed window that were not
            assigned to an event, i.e. duplicates of a real detection.
        unmatched_alarm_count: Alarms assigned to no event, equal to
            ``false_alarm_count + redundant_alarm_count``.
    """

    events: tuple[TruthEvent, ...]
    alarms: tuple[AlarmRecord, ...]
    matches: tuple[EventMatch, ...]
    false_alarm_count: int
    redundant_alarm_count: int
    unmatched_alarm_count: int

    @property
    def total_events(self) -> int:
        """Number of injected events."""
        return len(self.events)

    @property
    def detected_events(self) -> int:
        """Number of events that got an alarm assigned."""
        return sum(1 for match in self.matches if match.detected)

    @property
    def missed_events(self) -> int:
        """Number of events left without an alarm (false negatives)."""
        return self.total_events - self.detected_events

    @property
    def alarm_count(self) -> int:
        """Number of stored alarms."""
        return len(self.alarms)

    @property
    def detection_rate(self) -> float | None:
        """``detected / total``, or ``None`` when nothing was injected."""
        if not self.events:
            return None
        return self.detected_events / self.total_events

    def missed_event_ids(self) -> tuple[str, ...]:
        """Return the ids of the events that were not detected."""
        return tuple(match.event_id for match in self.matches if not match.detected)


@dataclass(frozen=True)
class ScenarioVerdict:
    """Outcome of judging one scenario against its own kind.

    Attributes:
        kind: ``normal``, ``degradation`` or ``fault``.
        passed: Whether the scenario met the criteria that apply to its kind.
        detection_rate: Passed through from the matching result.
        alarm_count: Total alarms in the run.
        false_alarm_count: Alarms outside every allowed event window.
        reasons: Human readable statements, one per criterion checked.
    """

    kind: str
    passed: bool
    detection_rate: float | None
    alarm_count: int
    false_alarm_count: int
    reasons: tuple[str, ...]


def _require_int(value: Any, name: str) -> int:
    """Return ``value`` as a non-boolean integer or raise.

    Args:
        value: Parsed JSON value.
        name: Field name used in the error message.

    Returns:
        The integer value.

    Raises:
        TruthError: if the value is not an integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TruthError(f"truth.{name} must be an integer, got {value!r}")
    return value


def _require_text(value: Any, name: str) -> str:
    """Return ``value`` as a non-empty string or raise.

    Args:
        value: Parsed JSON value.
        name: Field name used in the error message.

    Returns:
        The string value.

    Raises:
        TruthError: if the value is not a non-empty string.
    """
    if not isinstance(value, str) or not value.strip():
        raise TruthError(f"truth.{name} must be a non-empty string, got {value!r}")
    return value


def _event_from_mapping(payload: Mapping[str, Any], index: int) -> TruthEvent:
    """Build one :class:`TruthEvent` from a section 5.3 record.

    Args:
        payload: One element of the ``events`` array.
        index: Position in the array, used in error messages.

    Returns:
        The parsed event.

    Raises:
        TruthError: if a field is missing or malformed.
    """
    expected = {
        "event_id",
        "device_id",
        "type",
        "start_seq",
        "end_seq_exclusive",
        "amplitude",
    }
    missing = expected - set(payload)
    extra = set(payload) - expected
    if missing or extra:
        raise TruthError(
            f"truth.events[{index}] keys must be {sorted(expected)}, "
            f"missing {sorted(missing)}, unexpected {sorted(extra)}"
        )
    start_seq = _require_int(payload["start_seq"], f"events[{index}].start_seq")
    end_seq = _require_int(
        payload["end_seq_exclusive"], f"events[{index}].end_seq_exclusive"
    )
    if end_seq <= start_seq:
        raise TruthError(
            f"truth.events[{index}] must be a non-empty half open interval, got "
            f"[{start_seq}, {end_seq})"
        )
    amplitude = payload["amplitude"]
    if isinstance(amplitude, bool) or not isinstance(amplitude, (int, float)):
        raise TruthError(
            f"truth.events[{index}].amplitude must be a number, got {amplitude!r}"
        )
    return TruthEvent(
        event_id=_require_text(payload["event_id"], f"events[{index}].event_id"),
        device_id=_require_text(payload["device_id"], f"events[{index}].device_id"),
        fault_type=_require_text(payload["type"], f"events[{index}].type"),
        start_seq=start_seq,
        end_seq_exclusive=end_seq,
        amplitude=float(amplitude),
    )


def load_truth_events(path: Path) -> tuple[TruthEvent, ...]:
    """Read ``private_truth/faults.json`` and return its events.

    Only the validator calls this.  The file is written by the producer and is
    the single source of injected-event truth (``docs/interface.md`` 5.3).

    Args:
        path: Path to ``faults.json``.

    Returns:
        Events ordered by ``(start_seq, event_id)``.

    Raises:
        TruthError: if the document is not a valid section 5.3 payload.
        OSError: if the file cannot be read.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise TruthError(f"{path} must hold a JSON object, got {type(raw).__name__}")
    events = raw.get("events")
    if not isinstance(events, list):
        raise TruthError(f"{path} must carry an 'events' array")
    parsed = []
    for index, payload in enumerate(events):
        if not isinstance(payload, Mapping):
            raise TruthError(f"truth.events[{index}] must be a JSON object")
        parsed.append(_event_from_mapping(payload, index))
    return tuple(sorted(parsed, key=lambda item: (item.start_seq, item.event_id)))


def allowed_alarm_window(
    event: TruthEvent,
    window_points: int = WINDOW_POINTS_DEFAULT,
    max_detection_lag_windows: int = MAX_DETECTION_LAG_WINDOWS_DEFAULT,
) -> tuple[int, int]:
    """Return the half open range of alarm starts that may detect ``event``.

    ``docs/acceptance.md`` section 3 fixes the range as
    ``[floor(s/W) * W, ceil(e/W) * W + lag * W)``: every window that touches
    the event, plus ``lag`` windows of grace after it ends.

    Args:
        event: The injected event.
        window_points: Analysis window width ``W``.
        max_detection_lag_windows: Grace in windows after the event ends.

    Returns:
        ``(first_allowed_start, one_past_last_allowed_start)``.

    Raises:
        ValueError: if ``window_points`` is not positive or the lag is negative.
    """
    if window_points <= 0:
        raise ValueError(f"window_points must be positive, got {window_points}")
    if max_detection_lag_windows < 0:
        raise ValueError(
            "max_detection_lag_windows must not be negative, got "
            f"{max_detection_lag_windows}"
        )
    start = (event.start_seq // window_points) * window_points
    # Ceiling division in pure integers.  The event end is exclusive, so a
    # window that ends exactly at ``end_seq_exclusive`` must not be included.
    end = -((-event.end_seq_exclusive) // window_points) * window_points
    return start, end + max_detection_lag_windows * window_points


def _maximum_matching(adjacency: Sequence[Sequence[int]]) -> list[int]:
    """Return a maximum matching of events onto alarms.

    Kuhn's augmenting path algorithm.  Events are offered in the caller's
    order and each event tries its candidate alarms in the caller's order, so
    the result is deterministic: the first maximum matching reachable from the
    sorted inputs, which is the tie break section 3 asks for.

    Args:
        adjacency: For each event, the alarm indices inside its window, sorted.

    Returns:
        For each event, the matched alarm index or ``-1`` when unmatched.
    """
    owner: dict[int, int] = {}
    assignment = [-1] * len(adjacency)

    def _augment(event_index: int, seen: set[int]) -> bool:
        for alarm_index in adjacency[event_index]:
            if alarm_index in seen:
                continue
            seen.add(alarm_index)
            holder = owner.get(alarm_index)
            if holder is None or _augment(holder, seen):
                owner[alarm_index] = event_index
                assignment[event_index] = alarm_index
                return True
        return False

    for event_index in range(len(adjacency)):
        _augment(event_index, set())
    return assignment


def _sort_alarms(alarms: Iterable[AlarmRecord]) -> tuple[AlarmRecord, ...]:
    """Return alarms in the deterministic order section 3 prescribes."""
    return tuple(
        sorted(
            alarms,
            key=lambda alarm: (
                alarm.start_seq,
                alarm.rule_id,
                alarm.device_id,
                alarm.level,
            ),
        )
    )


def match_alarms(
    events: Sequence[TruthEvent],
    alarms: Iterable[AlarmRecord],
    *,
    window_points: int = WINDOW_POINTS_DEFAULT,
    max_detection_lag_windows: int = MAX_DETECTION_LAG_WINDOWS_DEFAULT,
) -> MatchResult:
    """Match stored alarms against injected events.

    Args:
        events: Injected events for this run.
        alarms: Every alarm stored for the run.
        window_points: Analysis window width ``W``.
        max_detection_lag_windows: Grace in windows after an event ends.

    Returns:
        The :class:`MatchResult` with per-event verdicts and the three alarm
        counts section 3 requires.

    Raises:
        ValueError: if ``window_points`` is not positive.
    """
    ordered_events = tuple(
        sorted(events, key=lambda item: (item.start_seq, item.event_id))
    )
    ordered_alarms = _sort_alarms(alarms)

    windows = [
        allowed_alarm_window(event, window_points, max_detection_lag_windows)
        for event in ordered_events
    ]
    adjacency: list[list[int]] = []
    for event, (low, high) in zip(ordered_events, windows):
        adjacency.append(
            [
                index
                for index, alarm in enumerate(ordered_alarms)
                if alarm.device_id == event.device_id and low <= alarm.start_seq < high
            ]
        )

    assignment = _maximum_matching(adjacency)

    matches = []
    for index, event in enumerate(ordered_events):
        low, high = windows[index]
        in_window = [
            alarm.start_seq
            for alarm in ordered_alarms
            if alarm.device_id == event.device_id and low <= alarm.start_seq < high
        ]
        alarm_index = assignment[index]
        matched = ordered_alarms[alarm_index] if alarm_index >= 0 else None
        matches.append(
            EventMatch(
                event_id=event.event_id,
                fault_type=event.fault_type,
                start_seq=event.start_seq,
                end_seq_exclusive=event.end_seq_exclusive,
                allowed_start=low,
                allowed_end_exclusive=high,
                matched_alarm_start=None if matched is None else matched.start_seq,
                matched_alarm_rule=None if matched is None else matched.rule_id,
                first_alarm_start=min(in_window) if in_window else None,
            )
        )

    matched_alarm_indexes = {index for index in assignment if index >= 0}
    false_alarms = 0
    redundant = 0
    for index, alarm in enumerate(ordered_alarms):
        if index in matched_alarm_indexes:
            continue
        inside = any(
            alarm.device_id == event.device_id and low <= alarm.start_seq < high
            for event, (low, high) in zip(ordered_events, windows)
        )
        if inside:
            redundant += 1
        else:
            false_alarms += 1

    return MatchResult(
        events=ordered_events,
        alarms=ordered_alarms,
        matches=tuple(matches),
        false_alarm_count=false_alarms,
        redundant_alarm_count=redundant,
        unmatched_alarm_count=false_alarms + redundant,
    )


def median_halves(scores: Sequence[float]) -> tuple[float | None, float | None]:
    """Return the medians of the first and second half of ``scores``.

    The degradation criterion of ``docs/acceptance.md`` section 3 compares the
    second half of the run against the first half, so the split is by position
    and not by score.

    Args:
        scores: Health scores in window order.

    Returns:
        ``(first_half_median, second_half_median)``, each ``None`` when that
        half holds no sample.
    """
    ordered = list(scores)
    if len(ordered) < 2:
        return (None, None)
    half = len(ordered) // 2
    first = statistics.median(ordered[:half]) if half else None
    second = statistics.median(ordered[half:]) if len(ordered) - half else None
    return (first, second)


def _evaluate_normal(result: MatchResult) -> ScenarioVerdict:
    """Judge a normal scenario: zero alarms and a null detection rate."""
    reasons = [
        f"alarm_count must be 0 for a normal scenario, observed {result.alarm_count}",
        "detection_rate must be null when no event was injected",
    ]
    passed = result.alarm_count == 0 and result.detection_rate is None
    return ScenarioVerdict(
        kind=SCENARIO_NORMAL,
        passed=passed,
        detection_rate=result.detection_rate,
        alarm_count=result.alarm_count,
        false_alarm_count=result.false_alarm_count,
        reasons=tuple(reasons),
    )


def _evaluate_degradation(
    result: MatchResult, health_scores: Sequence[float]
) -> ScenarioVerdict:
    """Judge a degradation scenario by trend, never by the zero-alarm gate.

    ``docs/acceptance.md`` section 3 requires the second half of the run to
    score lower than the first half.  Alarms raised by a genuine degradation
    trend are reported but do not fail the scenario, and they must not be
    folded into the normal-scenario false-alarm tally.
    """
    first, second = median_halves(health_scores)
    trend_ok = first is not None and second is not None and second < first
    reasons = [
        "detection_rate must be null: a degradation scenario injects no event",
        (
            "second-half health median must be below the first-half median, "
            f"observed first={first!r} second={second!r}"
        ),
    ]
    passed = result.detection_rate is None and trend_ok
    return ScenarioVerdict(
        kind=SCENARIO_DEGRADATION,
        passed=passed,
        detection_rate=result.detection_rate,
        alarm_count=result.alarm_count,
        false_alarm_count=result.false_alarm_count,
        reasons=tuple(reasons),
    )


def _evaluate_fault(result: MatchResult, expected_events: int) -> ScenarioVerdict:
    """Judge a fault scenario: every injected event must be detected."""
    reasons = [
        f"injected events must be {expected_events}, observed {result.total_events}",
        (
            "detection_rate must be 1.0 (TP=total, FN=0), observed "
            f"{result.detection_rate!r} with {result.missed_events} missed"
        ),
    ]
    passed = result.total_events == expected_events and result.detection_rate == 1.0
    return ScenarioVerdict(
        kind=SCENARIO_FAULT,
        passed=passed,
        detection_rate=result.detection_rate,
        alarm_count=result.alarm_count,
        false_alarm_count=result.false_alarm_count,
        reasons=tuple(reasons),
    )


def evaluate_scenario(
    kind: str,
    result: MatchResult,
    *,
    expected_events: int = 0,
    health_scores: Sequence[float] = (),
) -> ScenarioVerdict:
    """Judge one run against the criteria of its own scenario kind.

    The three kinds are kept strictly apart, as the review of Issue #9
    requires: a normal scenario fails on any alarm, a degradation scenario is
    judged only by its trend, and a fault scenario must detect every injected
    event.  False alarms are always reported, but only the normal scenario
    turns them into a failure.

    Args:
        kind: One of ``normal``, ``degradation`` or ``fault``.
        result: The matching result of the run.
        expected_events: Number of events the configuration injects.
        health_scores: Health scores in window order, used by ``degradation``.

    Returns:
        The :class:`ScenarioVerdict` for the run.

    Raises:
        ValueError: if ``kind`` is not one of the three known kinds.
    """
    if kind == SCENARIO_NORMAL:
        return _evaluate_normal(result)
    if kind == SCENARIO_DEGRADATION:
        return _evaluate_degradation(result, health_scores)
    if kind == SCENARIO_FAULT:
        return _evaluate_fault(result, expected_events)
    raise ValueError(
        f"unknown scenario kind {kind!r}; expected one of "
        f"{SCENARIO_NORMAL!r}, {SCENARIO_DEGRADATION!r}, {SCENARIO_FAULT!r}"
    )
