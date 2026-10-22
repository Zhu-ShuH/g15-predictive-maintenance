"""Yellow/red alarm evaluation with debounce and recovery (Issue #7, owner B2).

Implements ``docs/interface.md`` section 8.4 on top of the health score:

* the level of a window follows from the two configured thresholds, so a lower
  score can never produce a milder alarm (``thresholds.red < thresholds.yellow``
  is enforced when the rule file is parsed);
* ``consecutive_windows`` breaches must accumulate before an alarm is raised;
* ``recovery_windows`` clean windows must accumulate before an active alarm is
  cleared, which is the hysteresis that stops a score hovering at the threshold
  from flapping;
* alarms are **edge triggered**: one alarm per rise, plus one more if the state
  escalates from yellow to red. A fault lasting a hundred windows therefore
  yields one or two alarms, not a hundred near-duplicates. Repeated alarms
  inside a fault window are the exception the verifier counts separately
  (``docs/acceptance.md`` section 3, ``redundant_alarm_count``), not the default.

The state is a plain frozen dataclass so that C2 can persist it in SQLite and
rebuild it after ``kill -9``: nothing here depends on process memory or on the
wall clock. The alarm dedupe key is ``(device_id, rule_id, start_seq)`` and
:meth:`Alarm.dedupe_key` builds it; ``device_id`` is attached by the consumer,
which is the only layer that knows which run is being consumed.

The state also records the rule identity that produced it. Without that, a state
persisted under one ``scoring_version`` would silently carry its
``consecutive_breaches`` into a run scored by different thresholds, and a rule
change would fire an alarm on the first breaching window -- exactly the
"re-tune the yaml, no code change" workflow the version number exists to make
safe. A mismatch is therefore rejected unless the carried state has no history
to inherit (see :func:`_require_state_identity`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from g15.analysis.health import HealthResult, HealthRules

__all__ = [
    "LEVELS",
    "LEVEL_RANK",
    "Alarm",
    "AlarmState",
    "classify_level",
    "evaluate_alarm",
]

#: Alarm levels, from mild to severe.
LEVELS: Final[tuple[str, ...]] = ("yellow", "red")

#: Severity rank of each level; larger is worse.
LEVEL_RANK: Final[dict[str, int]] = {"yellow": 1, "red": 2}


@dataclass(frozen=True)
class Alarm:
    """One alarm raised for one window.

    Attributes:
        rule_id: Rule set that produced the alarm; part of the dedupe key.
        level: ``yellow`` or ``red``.
        start_seq: Absolute ``seq`` of the first sample of the offending window.
        health_score: Score that triggered the alarm.
        scoring_version: Version of the rules that produced the score.
        reason: Human readable text naming the threshold and the drivers.
        drivers: Feature names that pushed the score down, strongest first.
    """

    rule_id: str
    level: str
    start_seq: int
    health_score: float
    scoring_version: str
    reason: str
    drivers: tuple[str, ...]

    def dedupe_key(self, device_id: str) -> tuple[str, str, int]:
        """Return the uniqueness key of ``docs/acceptance.md`` section 3.

        Args:
            device_id: Device the window belongs to, from the run manifest.

        Returns:
            ``(device_id, rule_id, start_seq)``.
        """
        return (device_id, self.rule_id, self.start_seq)


@dataclass(frozen=True)
class AlarmState:
    """Alarm state carried from one window to the next.

    Every field is plain data so the consumer can store and restore it.

    Attributes:
        active_level: Level currently latched, or ``None`` when healthy.
        consecutive_breaches: Consecutive breaching windows in the current
            streak; drives the ``consecutive_windows`` debounce.
        normal_windows: Consecutive non-breaching windows; drives
            ``recovery_windows`` hysteresis.
        alarm_windows: Number of windows that raised an alarm since the current
            streak began (0 when healthy).
        last_window_start_seq: Last window position seen, used to reject
            out-of-order windows that would corrupt the counters.
        recovered_at_seq: Position of the window that cleared the last alarm, or
            ``None`` if no alarm has been cleared yet.
        scoring_version: ``scoring_version`` of the rules that produced this
            state. ``None`` means "not recorded yet" (a fresh streak, or a row
            written before the field existed); a value that disagrees with the
            rules is rejected once the state carries history.
        rule_id: ``rule_id`` of the rules that produced this state, validated
            alongside ``scoring_version``.
    """

    active_level: str | None = None
    consecutive_breaches: int = 0
    normal_windows: int = 0
    alarm_windows: int = 0
    last_window_start_seq: int | None = None
    recovered_at_seq: int | None = None
    scoring_version: str | None = None
    rule_id: str | None = None


def classify_level(health_score: float, rules: HealthRules) -> str | None:
    """Return the alarm level a score falls into.

    Args:
        health_score: Score in ``[0, 100]``.
        rules: Validated rule set carrying the thresholds.

    Returns:
        ``red`` at or below ``thresholds.red``, ``yellow`` at or below
        ``thresholds.yellow``, otherwise ``None``.
    """
    if health_score <= rules.red_threshold:
        return "red"
    if health_score <= rules.yellow_threshold:
        return "yellow"
    return None


def evaluate_alarm(
    health: HealthResult,
    state: AlarmState,
    rules: HealthRules,
) -> tuple[list[Alarm], AlarmState]:
    """Advance the alarm state machine by one window.

    Args:
        health: Health result of the window. ``window_start_seq`` is required:
            an alarm without a window position cannot be matched to a fault
            event, so a missing position is an error rather than a silent ``0``.
        state: State returned by the previous window (use ``AlarmState()`` for
            the first window of a run, or the state rebuilt from SQLite).
        rules: Validated rule set.

    Returns:
        ``(alarms, new_state)`` where ``alarms`` holds zero or one entry. More
        than one is impossible by construction: a window escalates at most one
        step, and each step is emitted once.

    Raises:
        ValueError: if the window position is missing or not increasing, if the
            health result was produced by a different rule set, or if ``state``
            was produced by a different rule set while still carrying history.
    """
    start_seq = _require_window(health)
    _require_monotonic(start_seq, state)
    _require_rule_identity(health, rules)
    _require_state_identity(state, rules)

    target = classify_level(health.health_score, rules)
    if target is None:
        return (), _healthy_window(start_seq, state, rules)

    breaches = state.consecutive_breaches + 1
    active = state.active_level
    alarms: list[Alarm] = []
    if breaches >= rules.consecutive_windows and _is_escalation(target, active):
        alarms.append(_build_alarm(health, target, rules))

    new_state = AlarmState(
        active_level=target if alarms else active,
        consecutive_breaches=breaches,
        normal_windows=0,
        alarm_windows=state.alarm_windows + (1 if alarms else 0),
        last_window_start_seq=start_seq,
        recovered_at_seq=state.recovered_at_seq,
        scoring_version=rules.scoring_version,
        rule_id=rules.rule_id,
    )
    return alarms, new_state


def _healthy_window(
    start_seq: int, state: AlarmState, rules: HealthRules
) -> AlarmState:
    """Advance the state machine on a window whose score is above thresholds.

    Args:
        start_seq: Position of the window.
        state: State carried in.
        rules: Validated rule set.

    Returns:
        The next state: still latched while fewer than ``recovery_windows``
        consecutive clean windows have been seen, cleared afterwards.
    """
    normal = state.normal_windows + 1
    if state.active_level is not None and normal >= rules.recovery_windows:
        return AlarmState(
            active_level=None,
            consecutive_breaches=0,
            normal_windows=normal,
            alarm_windows=0,
            last_window_start_seq=start_seq,
            recovered_at_seq=start_seq,
            scoring_version=rules.scoring_version,
            rule_id=rules.rule_id,
        )
    return AlarmState(
        active_level=state.active_level,
        consecutive_breaches=0,
        normal_windows=normal,
        alarm_windows=state.alarm_windows,
        last_window_start_seq=start_seq,
        recovered_at_seq=state.recovered_at_seq,
        scoring_version=rules.scoring_version,
        rule_id=rules.rule_id,
    )


def _is_escalation(target: str, active: str | None) -> bool:
    """Return whether ``target`` is a rise that deserves an alarm.

    Args:
        target: Level of the current window.
        active: Latched level, or ``None``.

    Returns:
        ``True`` when nothing is latched yet, or the window is more severe than
        the latched level. Equal or milder windows are the sustained part of an
        existing alarm and do not raise a second one.
    """
    if active is None:
        return True
    return LEVEL_RANK[target] > LEVEL_RANK[active]


def _build_alarm(health: HealthResult, level: str, rules: HealthRules) -> Alarm:
    """Build the alarm record of one window.

    Args:
        health: Health result of the window.
        level: Level to report.
        rules: Validated rule set.

    Returns:
        The populated :class:`Alarm`.
    """
    threshold = rules.red_threshold if level == "red" else rules.yellow_threshold
    return Alarm(
        rule_id=rules.rule_id,
        level=level,
        start_seq=int(health.window_start_seq),  # type: ignore[arg-type]
        health_score=health.health_score,
        scoring_version=health.scoring_version,
        reason=f"{health.explain()} <= {level} threshold {threshold:.2f}",
        drivers=health.drivers,
    )


def _require_window(health: HealthResult) -> int:
    """Return the window position or raise.

    Args:
        health: Health result to inspect.

    Returns:
        The non-negative window position.

    Raises:
        ValueError: if the position is missing or negative.
    """
    start_seq = health.window_start_seq
    if start_seq is None:
        raise ValueError(
            "health.window_start_seq is required to raise an alarm; pass "
            "window_start_seq to extract_features"
        )
    if start_seq < 0:
        raise ValueError(f"window_start_seq must be non-negative, got {start_seq}")
    return int(start_seq)


def _require_monotonic(start_seq: int, state: AlarmState) -> None:
    """Reject a window that is not after the previous one.

    Args:
        start_seq: Position of the current window.
        state: State carried in.

    Raises:
        ValueError: if the position is not strictly increasing, because replaying
            an old window would silently double-count a breach.
    """
    previous = state.last_window_start_seq
    if previous is not None and start_seq <= previous:
        raise ValueError(
            "window_start_seq must increase: got "
            f"{start_seq} after {previous}; windows must be evaluated once, in order"
        )


def _require_rule_identity(health: HealthResult, rules: HealthRules) -> None:
    """Check the result was scored by the same rule set.

    Args:
        health: Health result to inspect.
        rules: Rule set used to classify it.

    Raises:
        ValueError: if the score is not a finite number, or the scoring version
            or rule id disagree with ``rules``.
    """
    score = health.health_score
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError(f"health.health_score must be a number, got {score!r}")
    if not math.isfinite(float(score)):
        raise ValueError(f"health.health_score must be finite, got {score!r}")
    if health.scoring_version != rules.scoring_version:
        raise ValueError(
            "health was scored with scoring_version "
            f"{health.scoring_version!r}, rules are {rules.scoring_version!r}"
        )
    if health.rule_id != rules.rule_id:
        raise ValueError(
            f"health rule_id {health.rule_id!r} does not match rules "
            f"{rules.rule_id!r}"
        )


def _carries_history(state: AlarmState) -> bool:
    """Return whether the state holds counters that must not cross rule sets.

    Args:
        state: State carried in.

    Returns:
        ``True`` when the state is mid-streak or latched. A state with no active
        level, no accumulated breach and no alarm yet has nothing to inherit, so
        it can adopt the current rule identity instead of forcing a reset.
    """
    return (
        state.active_level is not None
        or state.consecutive_breaches != 0
        or state.alarm_windows != 0
    )


def _require_state_identity(state: AlarmState, rules: HealthRules) -> None:
    """Check the carried state was produced by the same rule set.

    The two failure modes are deliberately kept apart, because "I cannot tell
    which rules produced this" is a different bug from "this was produced by
    other rules":

    * a state that carries a streak or a latched alarm **must** name its rule set
      -- a missing identity on a state row is not evidence of a fresh start, it
      is evidence of a row written before the identity existed (a six-field
      ``state_json`` restored by an old consumer). Treating it as fresh would let
      the restored counters satisfy the debounce of whatever rules happen to be
      loaded now, which is exactly the silent inheritance the identity check
      exists to stop;
    * a state with nothing to inherit (no active level, no breach, no alarm) is
      accepted whatever it records, because it cannot change the outcome, and
      re-tagging it avoids blocking consumption after a retune.

    Args:
        state: State restored from the previous window or from SQLite.
        rules: Rule set scoring the current window.

    Raises:
        ValueError: if the state carries history but does not record both
            identity fields, or if a recorded field disagrees with ``rules``.
    """
    if not _carries_history(state):
        return
    history = (
        f"consecutive_breaches={state.consecutive_breaches}, "
        f"active_level={state.active_level!r}, alarm_windows={state.alarm_windows}"
    )
    for field, expected in (
        ("scoring_version", rules.scoring_version),
        ("rule_id", rules.rule_id),
    ):
        recorded = getattr(state, field)
        if recorded is None:
            raise ValueError(
                f"AlarmState carries history ({history}) but does not record "
                f"{field}, so the rule set that produced it cannot be "
                f"established. Restore both identity fields from state_json, or "
                f"start a new streak with AlarmState() if the old streak is to "
                f"be dropped; the current rules are {expected!r}"
            )
        if recorded != expected:
            raise ValueError(
                f"AlarmState was produced by {field}={recorded!r} but rules are "
                f"{expected!r}; the state still carries {history}. Start a new "
                "streak with AlarmState() when the rule set changes"
            )
