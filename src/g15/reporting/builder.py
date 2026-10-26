"""Report generation for the independent validator (Issue #9, owner B1/C5).

The builder is the only module that reads ``private_truth`` and the only one
that writes ``reports/summary.json`` / ``reports/summary.md``; both paths are
fixed by ``docs/interface.md`` sections 5.4 and 11.  Everything it reports is
read back from the run directory and the SQLite database -- nothing is
recomputed from the configuration, so the report cannot disagree with what the
pipeline actually stored.

Unknown metrics stay ``null``.  ``docs/interface.md`` section 5.4 forbids
writing ``0`` or ``100%`` for a value the run did not produce, and the
detection rate of a run with no injected event is exactly that case.
"""

from __future__ import annotations

import json
import platform
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from g15.pipeline.consumer import ConsumptionError, load_manifest_from_run_dir
from g15.reporting.metrics import (
    SCENARIO_DEGRADATION,
    SCENARIO_FAULT,
    SCENARIO_NORMAL,
    AlarmRecord,
    MatchResult,
    ScenarioVerdict,
    TruthError,
    evaluate_scenario,
    load_truth_events,
    match_alarms,
)

__all__ = ["REPORT_DIRECTORY", "ReportError", "build_report", "build_report_data"]

#: Directory holding the two report files (``docs/interface.md`` section 11).
REPORT_DIRECTORY: str = "reports"

#: File names inside :data:`REPORT_DIRECTORY`.
SUMMARY_JSON: str = "summary.json"
SUMMARY_MD: str = "summary.md"

#: Truth file written by the producer (``docs/interface.md`` section 5.3).
TRUTH_FILE: str = "private_truth/faults.json"

#: Log the producer appends its completion line to (section 11).
LOG_FILE: str = "logs/app.jsonl"

#: Report schema version, kept separate from the manifest ``schema_version``.
REPORT_SCHEMA_VERSION: str = "1.0"


class ReportError(Exception):
    """Raised when a run cannot be turned into a report."""


@dataclass(frozen=True)
class _RunFacts:
    """Everything read back from one run, before metrics are computed.

    Attributes:
        manifest: The validated run manifest.
        alarms: Stored alarm rows.
        health_scores: Health scores in window order.
        feature_windows: Number of stored feature windows.
        submitted_points: Samples covered by the shards the consumer recorded
            as committed, i.e. ``processed_chunks`` joined with the manifest.
        pending_parts: Manifest parts the consumer never recorded as processed.
            A ``runs`` row only proves the run was *started* in this database;
            until every part carries a ``processed_chunks`` row the run is
            partially consumed, and a partially consumed run must not be
            reported as a whole pass (review of Issue #9, R3).
        digest_mismatch_parts: Parts whose recorded digest differs from the
            manifest's.  The consumer refuses such a shard on the next pass, so
            the report must not score the rows already stored for it.
        analysed_points: Samples actually covered by stored feature windows,
            summed over the windows of every processed shard.
        dropped_points: Samples inside the processed shards that no stored
            window covers, i.e. ``submitted_points - analysed_points``.
        window_points: Analysis window width read back from the stored
            windows, or ``None`` when no window was written.
        elapsed_seconds: Producer measurement window, or ``None``.
        errors: Error lines found in the run log.
        consumed: Whether the database carries a ``runs`` row for this run,
            which is what makes the numbers above meaningful.
        scoring_version: ``runs.scoring_version`` of this run, or ``None``.
        declared_events: Event count the producer wrote to the run log, or
            ``None`` when the log carries no completion line.
    """

    manifest: Any
    alarms: tuple[AlarmRecord, ...]
    health_scores: tuple[float, ...]
    feature_windows: int
    submitted_points: int
    pending_parts: tuple[int, ...]
    digest_mismatch_parts: tuple[int, ...]
    analysed_points: int
    dropped_points: int
    window_points: int | None
    elapsed_seconds: float | None
    errors: tuple[str, ...]
    consumed: bool
    scoring_version: str | None
    declared_events: int | None


def _open_readonly(database_path: Path) -> sqlite3.Connection:
    """Open an existing database read-only, or raise :class:`ReportError`.

    The report must never mutate what it describes, so the connection is opened
    with ``mode=ro`` rather than through :func:`g15.storage.database.connect`.

    Args:
        database_path: Path to ``artifacts/results.sqlite``.

    Returns:
        A read-only connection.

    Raises:
        ReportError: if the file is missing or cannot be opened.
    """
    path = Path(database_path)
    if not path.is_file():
        raise ReportError(f"no database at {path}; run 'consume' before 'report'")
    try:
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ReportError(f"cannot open {path} read-only: {exc}") from exc


def _read_alarms(
    connection: sqlite3.Connection, run_id: str
) -> tuple[AlarmRecord, ...]:
    """Read every stored alarm of ``run_id``.

    Args:
        connection: Open read-only connection.
        run_id: The run to read.

    Returns:
        Alarm rows in database order.
    """
    rows = connection.execute(
        """
        SELECT device_id, rule_id, start_seq, level FROM alarms
        WHERE run_id = ? ORDER BY start_seq, rule_id
        """,
        (run_id,),
    ).fetchall()
    return tuple(
        AlarmRecord(
            device_id=str(row[0]),
            rule_id=str(row[1]),
            start_seq=int(row[2]),
            level=str(row[3]),
        )
        for row in rows
    )


def _read_health_scores(
    connection: sqlite3.Connection, run_id: str
) -> tuple[float, ...]:
    """Read health scores in window order.

    Args:
        connection: Open read-only connection.
        run_id: The run to read.

    Returns:
        Scores ordered by ``start_seq``.
    """
    rows = connection.execute(
        "SELECT health_score FROM feature_windows WHERE run_id = ? ORDER BY start_seq",
        (run_id,),
    ).fetchall()
    return tuple(float(row[0]) for row in rows)


def _read_processed_points(
    connection: sqlite3.Connection, run_id: str, manifest: Any
) -> tuple[int, tuple[int, ...], tuple[int, ...]]:
    """Return the samples covered by consumed shards and what is still missing.

    A shard counts as processed when the consumer recorded its digest in
    ``processed_chunks``; the row count comes from the manifest, which is the
    only place the per-shard geometry is stored.  This is the *submitted*
    count: it says the samples reached the consumer, not that every one of
    them entered a stored feature window.

    Two things make the count incomplete rather than merely smaller.  A part
    with no ``processed_chunks`` row was never consumed, and a part whose
    recorded digest differs from the manifest's was consumed from different
    bytes than the manifest describes -- the consumer refuses it on the next
    pass, so the rows already stored for it cannot be scored as this run
    (review of Issue #9, R3).

    Args:
        connection: Open read-only connection.
        run_id: The run to read.
        manifest: The run manifest.

    Returns:
        ``(submitted_points, pending_part_ids, digest_mismatch_part_ids)``.
    """
    rows = connection.execute(
        "SELECT part_id, sha256 FROM processed_chunks WHERE run_id = ?", (run_id,)
    ).fetchall()
    recorded = {int(row[0]): str(row[1]) for row in rows}
    pending: list[int] = []
    mismatched: list[int] = []
    submitted = 0
    for entry in manifest.parts:
        digest = recorded.get(entry.part_id)
        if digest is None:
            pending.append(entry.part_id)
            continue
        if digest != entry.sha256:
            mismatched.append(entry.part_id)
            continue
        submitted += int(entry.rows)
    return (submitted, tuple(pending), tuple(mismatched))


def _read_window_geometry(
    connection: sqlite3.Connection, run_id: str
) -> tuple[int | None, int]:
    """Return the stored window width and the samples the windows cover.

    The width comes from the geometry of the stored windows -- ``end_seq``
    minus ``start_seq`` -- because ``docs/interface.md`` section 9 has no
    column for it.  A report that assumed 256 would score a run consumed at a
    different width against the wrong grid, which is exactly the defect the
    review of Issue #9 calls out as R1.

    The covered sample count sums the width of every stored window.  Windows
    are disjoint by contract, so the sum is the number of samples that really
    entered analysis; the gap against the submitted shard rows is what the
    consumer dropped.

    Args:
        connection: Open read-only connection.
        run_id: The run to read.

    Returns:
        ``(window_points, covered_points)``; ``window_points`` is ``None``
        when no window was stored.

    Raises:
        ReportError: if the stored windows disagree on their width.
    """
    rows = connection.execute(
        """
        SELECT start_seq, end_seq_exclusive FROM feature_windows
        WHERE run_id = ? ORDER BY start_seq
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        return (None, 0)
    widths = {int(end) - int(start) for start, end in rows}
    if len(widths) != 1:
        raise ReportError(
            "the stored feature windows do not share one width "
            f"(observed {sorted(widths)}); the analysis grid cannot be trusted"
        )
    width = widths.pop()
    if width <= 0:
        raise ReportError(f"the stored feature windows have width {width}")
    return (width, width * len(rows))


def _read_log(run_dir: Path) -> tuple[float | None, tuple[str, ...], int | None]:
    """Read the run log for elapsed time, error lines and declared events.

    ``logs/app.jsonl`` is the only place the producer states how many events it
    injected (``run_complete.fault_events``), and that statement is what makes
    an absent truth file readable at all: the producer writes no
    ``private_truth/faults.json`` when it injected nothing, so "no file" alone
    is ambiguous and must not be guessed at (review of Issue #9, R2).

    Args:
        run_dir: The run directory.

    Returns:
        ``(elapsed_seconds, errors, declared_events)``.  ``elapsed_seconds``
        and ``declared_events`` are ``None`` when the log carries no
        ``run_complete`` line, which is what the report must show rather than
        inventing a number.
    """
    path = Path(run_dir) / LOG_FILE
    if not path.is_file():
        return (None, (), None)
    elapsed: float | None = None
    declared: int | None = None
    errors: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            errors.append(f"unparseable log line: {text[:120]}")
            continue
        if not isinstance(payload, Mapping):
            continue
        if payload.get("event") == "run_complete":
            if elapsed is None:
                milliseconds = payload.get("elapsed_ms")
                if isinstance(milliseconds, (int, float)) and not isinstance(
                    milliseconds, bool
                ):
                    elapsed = float(milliseconds) / 1000.0
            if declared is None:
                count = payload.get("fault_events")
                if isinstance(count, int) and not isinstance(count, bool):
                    declared = count
        if str(payload.get("level", "")).upper() in {"ERROR", "CRITICAL"}:
            errors.append(str(payload.get("message") or payload))
    return (elapsed, tuple(errors), declared)


def _read_run_row(
    connection: sqlite3.Connection, run_id: str
) -> tuple[bool, str | None]:
    """Return whether the database carries this run and its scoring version.

    The report is told both a run directory and a database path, and nothing
    stops a caller from pairing one run's directory with another run's
    database.  The ``runs`` row is the join between them: when it is missing,
    every count read from the other tables is zero because the run was never
    consumed there, not because the run is clean.  A report must never turn
    that into a pass (review of Issue #9, R3).

    Args:
        connection: Open read-only connection.
        run_id: ``manifest.run_id`` of the run being reported.

    Returns:
        ``(consumed, scoring_version)``.  ``consumed`` is ``False`` when no
        ``runs`` row carries this id, and then ``scoring_version`` is ``None``.
    """
    row = connection.execute(
        "SELECT scoring_version FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return (False, None)
    return (True, None if row[0] is None else str(row[0]))


def _gather(run_dir: Path, database_path: Path) -> _RunFacts:
    """Read one run into :class:`_RunFacts`.

    Args:
        run_dir: The run directory.
        database_path: Path to the run's SQLite database.

    Returns:
        The facts read back from disk.

    Raises:
        ReportError: if the manifest cannot be read or the database is unusable.
    """
    try:
        manifest = load_manifest_from_run_dir(Path(run_dir))
    except ConsumptionError as exc:
        raise ReportError(f"cannot read the manifest: {exc}") from exc

    connection = _open_readonly(database_path)
    try:
        consumed, scoring_version = _read_run_row(connection, manifest.run_id)
        alarms = _read_alarms(connection, manifest.run_id)
        health_scores = _read_health_scores(connection, manifest.run_id)
        feature_windows = len(health_scores)
        submitted_points, pending_parts, mismatch_parts = _read_processed_points(
            connection, manifest.run_id, manifest
        )
        window_points, analysed_points = _read_window_geometry(
            connection, manifest.run_id
        )
    except sqlite3.Error as exc:
        raise ReportError(f"cannot read {database_path}: {exc}") from exc
    finally:
        connection.close()

    elapsed, errors, declared_events = _read_log(run_dir)
    return _RunFacts(
        manifest=manifest,
        alarms=alarms,
        health_scores=health_scores,
        feature_windows=feature_windows,
        submitted_points=submitted_points,
        pending_parts=pending_parts,
        digest_mismatch_parts=mismatch_parts,
        analysed_points=analysed_points,
        dropped_points=submitted_points - analysed_points,
        window_points=window_points,
        elapsed_seconds=elapsed,
        errors=errors,
        consumed=consumed,
        scoring_version=scoring_version,
        declared_events=declared_events,
    )


def _scenario_kind(scenario_id: str, has_events: bool) -> str:
    """Classify a run into one of the three scenario kinds.

    The kind decides which acceptance criterion applies, so it is derived from
    the run itself: injected events mean a fault scenario, otherwise the
    scenario id names the profile (``acceptance_degradation_*`` is a
    degradation run, everything else without events is a normal run).

    Args:
        scenario_id: Scenario identifier from the manifest.
        has_events: Whether the run's truth file carries injected events.

    Returns:
        ``normal``, ``degradation`` or ``fault``.
    """
    if has_events:
        return SCENARIO_FAULT
    if SCENARIO_DEGRADATION in scenario_id.lower():
        return SCENARIO_DEGRADATION
    return SCENARIO_NORMAL


def _read_truth(
    run_dir: Path, run_id: str, declared_events: int | None
) -> tuple[Any, ...] | None:
    """Read the truth file of ``run_id``, or report that it is not verifiable.

    A missing file does **not** mean "no event injected": the producer only
    writes ``private_truth/faults.json`` when a scenario injects something, and
    a fault run whose truth was deleted, lost, or written by an older build
    looks exactly like that.  Treating absence as zero events would turn a
    failed fault run into a passing normal run, so absence is only accepted
    when the producer's own completion line says it injected zero events
    (review of Issue #9, R2).

    Args:
        run_dir: The run directory.
        run_id: ``manifest.run_id`` the truth must belong to.
        declared_events: ``run_complete.fault_events`` from the run log, or
            ``None`` when the log carries no completion line.

    Returns:
        The injected events, ``()`` when the log declares zero events, or
        ``None`` when the truth cannot be established.

    Raises:
        ReportError: if the file exists but does not match section 5.3 or
            belongs to a different run.
    """
    path = Path(run_dir) / TRUTH_FILE
    if not path.is_file():
        if declared_events == 0:
            return ()
        return None
    try:
        events = load_truth_events(path, expected_run_id=run_id)
    except TruthError as exc:
        raise ReportError(f"invalid truth file {path}: {exc}") from exc
    except OSError as exc:
        raise ReportError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        # ``json.loads`` raises ``JSONDecodeError`` (a ``ValueError``) for a
        # truncated or hand-edited file.  The public contract of this module is
        # ``ReportError``, so a caller must not have to know that a JSON syntax
        # error escapes as a different type (review of Issue #9, R2).
        raise ReportError(f"invalid truth file {path}: {exc}") from exc
    if declared_events is not None and declared_events != len(events):
        raise ReportError(
            f"invalid truth file {path}: the run log declares "
            f"{declared_events} injected event(s) but the file holds "
            f"{len(events)}"
        )
    return events


def _environment() -> dict[str, Any]:
    """Return the interpreter and platform facts the Markdown report cites."""
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "numpy": _numpy_version(),
    }


def _numpy_version() -> str | None:
    """Return the installed NumPy version, or ``None`` when it is absent."""
    try:
        import numpy  # pylint: disable=import-outside-toplevel
    except ImportError:  # pragma: no cover - numpy is a hard dependency
        return None
    return str(numpy.__version__)


def _counts(result: MatchResult) -> dict[str, Any]:
    """Return the matching counts section 5.4 and section 3 require."""
    return {
        "fault_events": result.total_events,
        "detected_events": result.detected_events,
        "missed_events": result.missed_events,
        "detection_rate": result.detection_rate,
        "false_alarm_count": result.false_alarm_count,
        "redundant_alarm_count": result.redundant_alarm_count,
        "unmatched_alarm_count": result.unmatched_alarm_count,
    }


def build_report_data(run_dir: Path, database_path: Path) -> dict[str, Any]:
    """Compute the report payload for one run.

    The report never decides "passed" from incomplete evidence.  Four facts
    are required before any verdict is produced: the run must be consumed in
    the database it was handed, *every* manifest part must carry a matching
    ``processed_chunks`` row (a ``runs`` row alone only means the run was
    started there), the truth must be readable and belong to this run (R2),
    and the analysis window width must come from the stored windows rather
    than a default (R1).  When one of them is missing the payload is still
    produced -- a diagnostic report is useful -- but ``verifiable`` is
    ``False``, ``passed`` is ``None``, and every metric that could not be
    established stays ``null``.

    Args:
        run_dir: The run directory.
        database_path: Path to the run's SQLite database.

    Returns:
        The ``summary.json`` payload as a plain dictionary.

    Raises:
        ReportError: if the run cannot be read, the truth file is invalid, or
            the database does not hold this run.
    """
    run_path = Path(run_dir)
    facts = _gather(run_path, database_path)
    run_id = str(facts.manifest.run_id)
    events = _read_truth(run_path, run_id, facts.declared_events)

    blockers: list[str] = []
    if not facts.consumed:
        blockers.append(
            f"the database {database_path} holds no runs row for run {run_id!r}; "
            "the run was not consumed there, so its windows, alarms and points "
            "are unknown rather than empty"
        )
    if events is None:
        blockers.append(
            "the run carries no private_truth/faults.json and its log does not "
            "declare zero injected events; a missing truth file cannot be read "
            "as 'no event injected', so the detection rate is unknown"
        )
    if facts.consumed and facts.pending_parts:
        blockers.append(
            f"the database holds processed rows for only part of run {run_id!r}: "
            f"manifest part(s) {list(facts.pending_parts)} carry no "
            "processed_chunks row, so the consumer never finished this run; a "
            "partially consumed run cannot be reported as a whole pass"
        )
    if facts.consumed and facts.digest_mismatch_parts:
        blockers.append(
            f"the database recorded a different digest for manifest part(s) "
            f"{list(facts.digest_mismatch_parts)} of run {run_id!r}; those rows "
            "were consumed from bytes the manifest does not describe, so they "
            "cannot be scored as this run"
        )
    if facts.consumed and facts.window_points is None:
        blockers.append(
            "the database holds no feature window for this run; the analysis "
            "window width is unknown and the allowed alarm window cannot be "
            "computed"
        )

    known_events: tuple[Any, ...] = events if events is not None else ()
    window_points = facts.window_points
    result = (
        match_alarms(known_events, facts.alarms, window_points=window_points)
        if window_points is not None
        else None
    )
    if events is None:
        kind = None
    else:
        kind = _scenario_kind(str(facts.manifest.scenario_id), bool(events))

    verdict: ScenarioVerdict | None = None
    if not blockers and result is not None and kind is not None:
        verdict = evaluate_scenario(
            kind,
            result,
            expected_events=len(known_events),
            health_scores=facts.health_scores,
        )
        if not verdict.passed:
            blockers.extend(verdict.reasons)

    scores = facts.health_scores
    matched_alarm_count = result.alarm_count if result is not None else None
    payload: dict[str, Any] = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "scenario_id": facts.manifest.scenario_id,
        "device_id": facts.manifest.device_id,
        "scenario_kind": kind,
        "verifiable": verdict is not None,
        "schema_version": facts.manifest.schema_version,
        "scoring_version": facts.scoring_version,
        "analysis_window_points": window_points,
        "total_points": facts.manifest.total_points,
        "scalar_value_count": 2 * facts.manifest.total_points,
        "processed_points": facts.submitted_points,
        "analysed_points": facts.analysed_points,
        "dropped_points": facts.dropped_points,
        "rejected_points": facts.manifest.total_points - facts.submitted_points,
        "pending_parts": list(facts.pending_parts),
        "digest_mismatch_parts": list(facts.digest_mismatch_parts),
        "feature_windows": facts.feature_windows,
        "alarm_count": matched_alarm_count,
        "dropped_alarm_count": (
            result.dropped_alarm_count if result is not None else None
        ),
        "health_score_min": min(scores) if scores else None,
        "health_score_max": max(scores) if scores else None,
        "elapsed_seconds": facts.elapsed_seconds,
        "errors": list(facts.errors),
        "passed": None if verdict is None else verdict.passed,
        "verdict_reasons": list(blockers),
        "events": [
            {
                "event_id": match.event_id,
                "type": match.fault_type,
                "start_seq": match.start_seq,
                "end_seq_exclusive": match.end_seq_exclusive,
                "allowed_start": match.allowed_start,
                "allowed_end_exclusive": match.allowed_end_exclusive,
                "detected": match.detected,
                "matched_alarm_start": match.matched_alarm_start,
                "matched_alarm_rule": match.matched_alarm_rule,
                "first_alarm_start": match.first_alarm_start,
                "lag_samples": match.lag_samples,
            }
            for match in (result.matches if result is not None else ())
        ],
    }
    if result is not None and events is not None:
        payload.update(_counts(result))
    else:
        # The truth is unknown, so nothing that depends on it may be stated:
        # not the event total, not the detection rate, and above all not a
        # false-alarm count -- an alarm that would have matched an event we
        # cannot see is not a false alarm (review of Issue #9, R2).  The alarm
        # side of the database is still a fact, so ``alarm_count`` and
        # ``dropped_alarm_count`` above stay as read.
        payload.update(
            {
                "fault_events": None,
                "detected_events": None,
                "missed_events": None,
                "detection_rate": None,
                "false_alarm_count": None,
                "redundant_alarm_count": None,
                "unmatched_alarm_count": None,
            }
        )
    return payload


def _format_rate(value: float | None) -> str:
    """Render a detection rate as a percentage, or ``null``.

    Args:
        value: Detection rate in ``[0, 1]``, or ``None``.

    Returns:
        ``null`` for ``None``; otherwise a percentage with two decimals.  The
        word ``null`` is deliberate: section 5.4 forbids substituting a number
        for a metric the run did not produce.
    """
    if value is None:
        return "null"
    return f"{value * 100.0:.2f}%"


def _format_optional(value: Any, template: str = "{}") -> str:
    """Render ``value`` with ``template``, or ``null`` when it is ``None``."""
    if value is None:
        return "null"
    return template.format(value)


def render_markdown(payload: Mapping[str, Any]) -> str:
    """Render ``summary.md`` from the JSON payload.

    The Markdown carries the same numbers as the JSON, plus the environment the
    report was produced in, because ``docs/interface.md`` section 5.4 requires
    both files to state the same measurement basis.

    Args:
        payload: The ``summary.json`` payload.

    Returns:
        The Markdown document.
    """
    environment = _environment()
    passed = payload["passed"]
    if passed is None:
        verdict_text = "**不可判定**（证据不全，见下方原因）"
    elif passed:
        verdict_text = "**通过**"
    else:
        verdict_text = "**未通过**"
    kind_text = "null" if payload["scenario_kind"] is None else payload["scenario_kind"]
    lines = [
        f"# 验证报告：{payload['run_id']}",
        "",
        "> 本文件由 `g15.reporting.builder` 从运行目录与 SQLite 数据库回读生成；",
        "> 检出率口径见 `docs/interface.md` §8.5 与 `docs/acceptance.md` §3。",
        "> `null` 表示该指标不存在或不可判定，不代表 0，也不代表 100%。",
        "",
        "## 1. 结论",
        "",
        f"- 场景类型：`{kind_text}`",
        f"- 可验证：**{'是' if payload['verifiable'] else '否'}**",
        f"- 判定：{verdict_text}",
    ]
    for reason in payload["verdict_reasons"]:
        lines.append(f"  - {reason}")
    lines += [
        "",
        "## 2. 关键指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 注入事件总数 | {_format_optional(payload['fault_events'])} |",
        f"| 已检出事件 | {_format_optional(payload['detected_events'])} |",
        f"| 漏报事件 | {_format_optional(payload['missed_events'])} |",
        f"| 检出率 | {_format_rate(payload['detection_rate'])} |",
        f"| 误报数 | {_format_optional(payload['false_alarm_count'])} |",
        f"| 冗余告警数 | {_format_optional(payload['redundant_alarm_count'])} |",
        f"| 未匹配告警数 | {_format_optional(payload['unmatched_alarm_count'])} |",
        f"| 参与匹配的告警数 | {_format_optional(payload['alarm_count'])} |",
        (
            "| 被丢弃的告警行（重复键/非法等级） | "
            f"{_format_optional(payload['dropped_alarm_count'])} |"
        ),
        "",
        "## 3. 运行口径",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| run_id | `{payload['run_id']}` |",
        f"| scenario_id | `{payload['scenario_id']}` |",
        f"| device_id | `{payload['device_id']}` |",
        f"| schema_version | `{payload['schema_version']}` |",
        f"| scoring_version | {_format_optional(payload['scoring_version'], '`{}`')} |",
        (
            "| analysis_window_points（从已存窗口读出） | "
            f"{_format_optional(payload['analysis_window_points'])} |"
        ),
        f"| total_points | {payload['total_points']} |",
        f"| scalar_value_count | {payload['scalar_value_count']} |",
        f"| processed_points（已提交片覆盖） | {payload['processed_points']} |",
        f"| analysed_points（真实进入窗口） | {payload['analysed_points']} |",
        f"| dropped_points（片内未成窗） | {payload['dropped_points']} |",
        f"| rejected_points（未消费的片） | {payload['rejected_points']} |",
        f"| pending_parts（无处理记录的片） | {payload['pending_parts'] or '无'} |",
        (
            "| digest_mismatch_parts（摘要与清单不符的片） | "
            f"{payload['digest_mismatch_parts'] or '无'} |"
        ),
        f"| feature_windows | {payload['feature_windows']} |",
        f"| health_score_min | {_format_optional(payload['health_score_min'])} |",
        f"| health_score_max | {_format_optional(payload['health_score_max'])} |",
        (
            "| elapsed_seconds（生产计时窗口） | "
            f"{_format_optional(payload['elapsed_seconds'])} |"
        ),
        "",
        "## 4. 逐事件匹配",
        "",
    ]
    if payload["events"]:
        lines += [
            "| event_id | 类型 | 事件区间 | 允许告警起点 | 已检出 | 匹配告警起点 | 首次告警窗口 | 数据时延（采样点） |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for event in payload["events"]:
            interval = f"[{event['start_seq']}, {event['end_seq_exclusive']})"
            allowed = f"[{event['allowed_start']}, {event['allowed_end_exclusive']})"
            lines.append(
                f"| {event['event_id']} | {event['type']} | {interval} | {allowed} | "
                f"{'是' if event['detected'] else '**否**'} | "
                f"{_format_optional(event['matched_alarm_start'])} | "
                f"{_format_optional(event['first_alarm_start'])} | "
                f"{_format_optional(event['lag_samples'])} |"
            )
    elif payload["fault_events"] is None:
        lines.append("真值不可用或事件未知，逐事件匹配无法进行；这不是「零事件」。")
    else:
        lines.append("本运行的真值文件明确记录零个注入事件。")
    lines += ["", "## 5. 错误记录", ""]
    if payload["errors"]:
        for error in payload["errors"]:
            lines.append(f"- {error}")
    else:
        lines.append("运行日志中没有 ERROR 级记录。")
    lines += [
        "",
        "## 6. 运行环境",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| Python | {environment['python']} ({environment['implementation']}) |",
        f"| NumPy | {_format_optional(environment['numpy'])} |",
        f"| 平台 | {environment['platform']} |",
        f"| 架构 | {environment['machine']} |",
        "",
        "> 本报告是单次运行的可复核证据，不是双 Linux 部署验收记录；",
        "> 正式验收须在 Ubuntu 24.04 与 RHEL 9 上按 `docs/acceptance.md` §1 另行留证。",
        "",
    ]
    return "\n".join(lines)


def build_report(run_dir: Path, database_path: Path) -> Path:
    """Write ``reports/summary.json`` and ``reports/summary.md`` for one run.

    This is the frozen signature of ``docs/interface.md`` section 6.

    Args:
        run_dir: The run directory.
        database_path: Path to the run's SQLite database.

    Returns:
        The path of ``reports/summary.json``.

    Raises:
        ReportError: if the run cannot be read, the truth file is invalid, or
            the report files cannot be written.
    """
    run_path = Path(run_dir)
    payload = build_report_data(run_path, database_path)
    report_dir = run_path / REPORT_DIRECTORY
    json_path = report_dir / SUMMARY_JSON
    markdown_path = report_dir / SUMMARY_MD
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"cannot write the report into {report_dir}: {exc}") from exc
    return json_path
