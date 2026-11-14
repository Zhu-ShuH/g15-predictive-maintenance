"""Public live reports from one actual read-only SQLite snapshot."""

from __future__ import annotations

import json
import math
from pathlib import Path
import stat

from g15.storage.database import connect_readonly
from .schema import STATES, load_live_session, utc_now


def render_live_report(
    runs_root: Path | str,
    run_id: str,
    device_id: str,
    extension: str,
    *,
    live_status: str | None = None,
) -> bytes:
    """Render an already-authorized exact pair; never read report/truth files.

    Counts and health aggregates share one read-only database transaction.
    Capture/pending counts are omitted because they belong to a separate
    runtime observation, not this committed database snapshot.
    """
    if extension not in {"json", "md"}:
        raise ValueError("unsupported live report format")
    root = Path(runs_root).resolve(strict=True)
    directory = root / run_id
    resolved = directory.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("live report resource escaped root")
    live = load_live_session(directory, run_id, device_id)
    for path in (directory / "artifacts", directory / "artifacts" / "results.sqlite"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("live report reparse paths are not supported")
    database = (directory / "artifacts" / "results.sqlite").resolve(strict=True)
    if not database.is_file() or not database.is_relative_to(resolved):
        raise ValueError("live report database escaped run")
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT schema_version,scoring_version,status,scenario_id,started_at "
            "FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        expected = (
            live["schema_version"],
            live["scoring_version"],
            "streaming",
            live["scenario_id"],
            live["started_at_utc"],
        )
        if row is None or tuple(row) != expected:
            raise ValueError("live report database identity mismatch")
        count, processed, minimum, maximum, mean = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(end_seq_exclusive-start_seq),0),"
            "MIN(health_score),MAX(health_score),AVG(health_score) "
            "FROM feature_windows WHERE run_id=? AND device_id=?",
            (run_id, device_id),
        ).fetchone()
        alarms = connection.execute(
            "SELECT COUNT(*) FROM alarms WHERE run_id=? AND device_id=?",
            (run_id, device_id),
        ).fetchone()[0]
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    if any(
        value is not None and not math.isfinite(value)
        for value in (minimum, maximum, mean)
    ):
        raise ValueError("invalid live health aggregate")
    status = live["status"] if live_status is None else live_status
    if status not in STATES:
        raise ValueError("invalid live report state")
    report = {
        "report_schema_version": "live-1",
        "run_id": run_id,
        "device_id": device_id,
        "scenario_id": live["scenario_id"],
        "scenario_kind": None,
        "source_kind": "wall_clock_simulator",
        "generated_at_utc": utc_now(),
        "verifiable": False,
        "passed": None,
        "detection_rate": None,
        "false_alarm_count": None,
        "fault_events": None,
        "detected_events": None,
        "missed_events": None,
        "redundant_alarm_count": None,
        "unmatched_alarm_count": None,
        "feature_windows": count,
        "alarm_count": alarms,
        "windows_written": count,
        "alarms_written": alarms,
        "processed_samples": processed,
        "health_score_min": minimum,
        "health_score_max": maximum,
        "health_score_mean": mean,
        "health_summary": {
            "count": count,
            "minimum": minimum,
            "maximum": maximum,
            "mean": mean,
        },
        "live_status": status,
        "count_basis": "single_readonly_sqlite_transaction",
        "dropped_points": None,
        "verdict_reasons": [
            "Live simulation has no independent truth or held-out acceptance evidence."
        ],
    }
    if extension == "json":
        return (
            json.dumps(report, ensure_ascii=True, sort_keys=True, allow_nan=False)
            + "\n"
        ).encode("utf-8")
    lines = [
        "# Live session snapshot",
        "",
        f"| run_id | `{run_id}` |",
        f"| device_id | `{device_id}` |",
        "",
        "verifiable: false; passed: null; detection_rate: null; false_alarm_count: null.",
        "",
        f"Committed windows: {count}; alarms: {alarms}; processed samples: {processed}.",
        f"Health score minimum: {minimum}; maximum: {maximum}; mean: {mean}.",
        "",
        "No independent truth or formal detection claim.",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")
