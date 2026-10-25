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
        processed_points: Samples covered by the shards the consumer recorded.
        elapsed_seconds: Producer measurement window, or ``None``.
        errors: Error lines found in the run log.
    """

    manifest: Any
    alarms: tuple[AlarmRecord, ...]
    health_scores: tuple[float, ...]
    feature_windows: int
    processed_points: int
    elapsed_seconds: float | None
    errors: tuple[str, ...]


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
) -> int:
    """Return the number of samples covered by consumed shards.

    A shard counts as processed when the consumer recorded its digest in
    ``processed_chunks``; the row count comes from the manifest, which is the
    only place the per-shard geometry is stored.

    Args:
        connection: Open read-only connection.
        run_id: The run to read.
        manifest: The run manifest.

    Returns:
        Sum of ``rows`` over the processed shards.
    """
    rows = connection.execute(
        "SELECT part_id FROM processed_chunks WHERE run_id = ?", (run_id,)
    ).fetchall()
    processed_ids = {int(row[0]) for row in rows}
    return sum(
        int(entry.rows) for entry in manifest.parts if entry.part_id in processed_ids
    )


def _read_log(run_dir: Path) -> tuple[float | None, tuple[str, ...]]:
    """Read the run log for the producer's elapsed time and error lines.

    Args:
        run_dir: The run directory.

    Returns:
        ``(elapsed_seconds, errors)``.  ``elapsed_seconds`` is ``None`` when the
        log carries no ``run_complete`` line, which is what the report must
        show rather than inventing a number.
    """
    path = Path(run_dir) / LOG_FILE
    if not path.is_file():
        return (None, ())
    elapsed: float | None = None
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
        if payload.get("event") == "run_complete" and elapsed is None:
            milliseconds = payload.get("elapsed_ms")
            if isinstance(milliseconds, (int, float)) and not isinstance(
                milliseconds, bool
            ):
                elapsed = float(milliseconds) / 1000.0
        if str(payload.get("level", "")).upper() in {"ERROR", "CRITICAL"}:
            errors.append(str(payload.get("message") or payload))
    return (elapsed, tuple(errors))


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
        alarms = _read_alarms(connection, manifest.run_id)
        health_scores = _read_health_scores(connection, manifest.run_id)
        feature_windows = len(health_scores)
        processed_points = _read_processed_points(connection, manifest.run_id, manifest)
    except sqlite3.Error as exc:
        raise ReportError(f"cannot read {database_path}: {exc}") from exc
    finally:
        connection.close()

    elapsed, errors = _read_log(run_dir)
    return _RunFacts(
        manifest=manifest,
        alarms=alarms,
        health_scores=health_scores,
        feature_windows=feature_windows,
        processed_points=processed_points,
        elapsed_seconds=elapsed,
        errors=errors,
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


def _read_truth(run_dir: Path) -> tuple[Any, ...]:
    """Read the truth file, treating "no file" as "no event".

    Args:
        run_dir: The run directory.

    Returns:
        The injected events, empty when the producer wrote no truth file.

    Raises:
        ReportError: if the file exists but does not match section 5.3.
    """
    path = Path(run_dir) / TRUTH_FILE
    if not path.is_file():
        return ()
    try:
        return load_truth_events(path)
    except TruthError as exc:
        raise ReportError(f"invalid truth file {path}: {exc}") from exc
    except OSError as exc:
        raise ReportError(f"cannot read {path}: {exc}") from exc


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

    Args:
        run_dir: The run directory.
        database_path: Path to the run's SQLite database.

    Returns:
        The ``summary.json`` payload as a plain dictionary.

    Raises:
        ReportError: if the run cannot be read or the truth file is invalid.
    """
    run_path = Path(run_dir)
    facts = _gather(run_path, database_path)
    events = _read_truth(run_path)
    result = match_alarms(events, facts.alarms)
    kind = _scenario_kind(facts.manifest.scenario_id, bool(events))
    verdict: ScenarioVerdict = evaluate_scenario(
        kind,
        result,
        expected_events=len(events),
        health_scores=facts.health_scores,
    )

    scores = facts.health_scores
    payload: dict[str, Any] = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run_id": facts.manifest.run_id,
        "scenario_id": facts.manifest.scenario_id,
        "device_id": facts.manifest.device_id,
        "scenario_kind": kind,
        "schema_version": facts.manifest.schema_version,
        "scoring_version": _scoring_version(run_path),
        "total_points": facts.manifest.total_points,
        "scalar_value_count": 2 * facts.manifest.total_points,
        "processed_points": facts.processed_points,
        "rejected_points": facts.manifest.total_points - facts.processed_points,
        "feature_windows": facts.feature_windows,
        "alarm_count": result.alarm_count,
        "health_score_min": min(scores) if scores else None,
        "health_score_max": max(scores) if scores else None,
        "elapsed_seconds": facts.elapsed_seconds,
        "errors": list(facts.errors),
        "passed": verdict.passed,
        "verdict_reasons": list(verdict.reasons),
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
            for match in result.matches
        ],
    }
    payload.update(_counts(result))
    return payload


def _scoring_version(run_dir: Path) -> str | None:
    """Return the scoring version the run was consumed with, or ``None``.

    Args:
        run_dir: The run directory.

    Returns:
        ``runs.scoring_version`` from the database, or ``None`` when the run has
        not been consumed yet.
    """
    database = Path(run_dir) / "artifacts" / "results.sqlite"
    if not database.is_file():
        return None
    try:
        connection = _open_readonly(database)
    except ReportError:
        return None
    try:
        row = connection.execute("SELECT scoring_version FROM runs LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return None if row is None else str(row[0])


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
    lines = [
        f"# 验证报告：{payload['run_id']}",
        "",
        "> 本文件由 `g15.reporting.builder` 从运行目录与 SQLite 数据库回读生成；",
        "> 检出率口径见 `docs/interface.md` §8.5 与 `docs/acceptance.md` §3。",
        "> `null` 表示该指标不存在，不代表 0，也不代表 100%。",
        "",
        "## 1. 结论",
        "",
        f"- 场景类型：`{payload['scenario_kind']}`",
        f"- 判定：**{'通过' if payload['passed'] else '未通过'}**",
    ]
    for reason in payload["verdict_reasons"]:
        lines.append(f"  - {reason}")
    lines += [
        "",
        "## 2. 关键指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 注入事件总数 | {payload['fault_events']} |",
        f"| 已检出事件 | {payload['detected_events']} |",
        f"| 漏报事件 | {payload['missed_events']} |",
        f"| 检出率 | {_format_rate(payload['detection_rate'])} |",
        f"| 误报数 | {payload['false_alarm_count']} |",
        f"| 冗余告警数 | {payload['redundant_alarm_count']} |",
        f"| 未匹配告警数 | {payload['unmatched_alarm_count']} |",
        f"| 告警总数 | {payload['alarm_count']} |",
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
        f"| total_points | {payload['total_points']} |",
        f"| scalar_value_count | {payload['scalar_value_count']} |",
        f"| processed_points | {payload['processed_points']} |",
        f"| rejected_points | {payload['rejected_points']} |",
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
    else:
        lines.append(
            "本运行没有注入事件（`private_truth/faults.json` 不存在或事件为空）。"
        )
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
