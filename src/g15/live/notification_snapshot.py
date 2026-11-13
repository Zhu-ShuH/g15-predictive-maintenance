"""Complete phone-notification evidence from one public live SQLite snapshot."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3

from g15.notifications.advice import maintenance_advice
from g15.notifications.models import AlertSnapshot
from g15.storage.repository import decode_state_json
from .reporting import (
    _format_live_report,
    _live_report_transaction,
    _read_live_report,
)

_MAX_SOURCE_ROWS = 100
_MAX_SOURCE_TEXT = 65536
_LATEST_SOURCES = """
    FROM alarms AS source
    WHERE source.run_id=? AND source.device_id=?
      AND source.level IN ('red','yellow')
      AND NOT EXISTS (
        SELECT 1 FROM alarms AS newer
        WHERE newer.run_id=source.run_id AND newer.device_id=source.device_id
          AND newer.rule_id=source.rule_id AND newer.level=source.level
          AND newer.start_seq>source.start_seq
      )
"""


def read_notification_snapshot(
    runs_root: Path | str,
    run_id: str,
    device_id: str,
    *,
    live_status: str | None = None,
) -> AlertSnapshot:
    """Return one exact pair's committed report, red-event count and advice.

    The caller must authorize the pair before invoking this public-data reader.
    Red events are persisted ``alarms.level='red'`` rows across the whole run,
    independent of feature-window counts and browser pagination. Every report
    aggregate and advice source shares the same read-only transaction. Invalid
    paths, identities, source data or source sets exceeding 100 rows fail closed.
    No truth or persisted report file is read and no notification is sent here.
    """
    with _live_report_transaction(runs_root, run_id, device_id) as (live, connection):
        report = _read_live_report(
            connection, live, run_id, device_id, live_status=live_status
        )
        red_count = connection.execute(
            "SELECT COUNT(*) FROM alarms "
            "WHERE run_id=? AND device_id=? AND level='red'",
            (run_id, device_id),
        ).fetchone()[0]
        latest_red = connection.execute(
            "SELECT rule_id,start_seq FROM alarms "
            "WHERE run_id=? AND device_id=? AND level='red' "
            "ORDER BY start_seq DESC,rule_id DESC LIMIT 1",
            (run_id, device_id),
        ).fetchone()
        last_red_id = None
        if latest_red is not None:
            key = (_text(latest_red[0], "rule_id"), _position(latest_red[1]))
            encoded = json.dumps(key, ensure_ascii=True, separators=(",", ":"))
            last_red_id = "red:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()
        alarms, states = _read_sources(connection, run_id, device_id)
        snapshot = AlertSnapshot(
            run_id=run_id,
            device_id=device_id,
            red_count=red_count,
            last_red_id=last_red_id,
            report_json=report,
            report_markdown=_format_live_report(report, "md").decode("utf-8"),
            advice=maintenance_advice(alarms, states),
            complete=True,
        )
    return snapshot


def _read_sources(
    connection: sqlite3.Connection, run_id: str, device_id: str
) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    parameters = (run_id, device_id)
    alarm_count = connection.execute(
        "SELECT COUNT(*) " + _LATEST_SOURCES, parameters
    ).fetchone()[0]
    state_count = connection.execute(
        "SELECT COUNT(*) FROM alarm_states WHERE run_id=? AND device_id=?",
        parameters,
    ).fetchone()[0]
    if alarm_count + state_count > _MAX_SOURCE_ROWS:
        raise ValueError("notification snapshot source set is incomplete")
    alarm_rows = connection.execute(
        "SELECT source.rule_id,source.start_seq,source.level,source.reason,"
        "source.created_at "
        + _LATEST_SOURCES
        + " ORDER BY source.start_seq DESC,source.rule_id DESC,source.level "
        "LIMIT ?",
        (*parameters, _MAX_SOURCE_ROWS),
    ).fetchall()
    state_rows = connection.execute(
        "SELECT rule_id,last_end_seq_exclusive,state_json FROM alarm_states "
        "WHERE run_id=? AND device_id=? ORDER BY rule_id LIMIT ?",
        (*parameters, _MAX_SOURCE_ROWS),
    ).fetchall()
    if len(alarm_rows) != alarm_count or len(state_rows) != state_count:
        raise ValueError("notification snapshot source set is incomplete")
    alarms = tuple(
        {
            "rule_id": _text(row[0], "rule_id"),
            "start_seq": _position(row[1]),
            "level": row[2],
            "reason": _text(row[3], "reason"),
            "created_at": _text(row[4], "created_at"),
        }
        for row in alarm_rows
    )
    return alarms, tuple(_state_item(row) for row in state_rows)


def _state_item(row: tuple[object, ...]) -> dict[str, object]:
    rule_id = _text(row[0], "rule_id")
    text = _text(row[2], "state_json")
    # The storage decoder validates versions and field types; additionally
    # reject duplicate JSON fields rather than let a final value hide damage.
    json.loads(text, object_pairs_hook=_unique_object)
    state = decode_state_json(text)
    if state.rule_id is not None and state.rule_id != rule_id:
        raise ValueError("notification snapshot state rule identity mismatch")
    values = asdict(state)
    if state.active_level not in (None, "yellow", "red") or any(
        values[name] < 0
        for name in ("consecutive_breaches", "normal_windows", "alarm_windows")
    ):
        raise ValueError("invalid notification source state")
    for name in ("last_window_start_seq", "recovered_at_seq"):
        if values[name] is not None:
            _position(values[name])
    return {
        "rule_id": rule_id,
        "last_end_seq_exclusive": _position(row[1]),
        "state": values,
    }


def _position(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid notification source position")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_SOURCE_TEXT:
        raise ValueError(f"invalid notification source {label}")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate notification source state field")
        result[key] = value
    return result
