"""Read-only query over the committed run database.

This module is the application-query half of Issue #135 D0. It opens the
database only through connect_readonly() and issues plain SELECT statements;
it never runs DDL, DML, ANALYZE or any write. It reads from three tables:

* runs          - the run identity row (manifest_schema_version lives here)
* feature_windows - paged by start_seq DESC
* alarms        - paged by start_seq DESC, rule_id DESC
* alarm_states  - the health state projection (allow-listed fields only)

Sensitive fields are never returned: raw state_json, reset_event,
reset_history, previous_state_json, free-text alarm reason, health_explain and
anything derived from private_truth are all absent from every response.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from g15.storage.database import DatabaseError, connect_readonly
from g15.storage.repository import (
    FEATURES_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
)
from g15.query.errors import AuthorizationError

#: The eight allow-listed fields of a health state projection, in order.
STATE_PROJECTION_FIELDS = (
    "active_level",
    "consecutive_breaches",
    "normal_windows",
    "alarm_windows",
    "last_window_start_seq",
    "recovered_at_seq",
    "scoring_version",
    "rule_id",
)

#: Columns selected from feature_windows for one page of windows.
_WINDOW_COLS = "start_seq, end_seq_exclusive, health_score, features_json"

#: Columns selected from alarms for one page of alarms.
_ALARM_COLS = "rule_id, start_seq, level"


@dataclass(frozen=True)
class WindowItem:
    start_seq: int
    end_seq_exclusive: int
    health_score: float
    features_version: str
    scoring_version: object
    rule_id: object


@dataclass(frozen=True)
class AlarmItem:
    rule_id: str
    start_seq: int
    level: str


@dataclass(frozen=True)
class HealthView:
    manifest_schema_version: str
    state_schema_version: str
    state: dict


def open_readonly(database_path: Path | str) -> sqlite3.Connection:
    """Open an existing database read-only, converting errors to 404.

    The path is expected to be a file that already exists; connect_readonly
    raises DatabaseError when it is not, which we surface as AuthorizationError
    (404) so a missing resource is never distinguishable from a forbidden one.
    """
    try:
        return connect_readonly(database_path)
    except DatabaseError as exc:
        raise AuthorizationError("resource is not available read-only") from exc


def read_run_identity(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT scenario_id, schema_version, scoring_version, status, started_at "
        "FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "scenario_id": row[0],
        "schema_version": row[1],
        "scoring_version": row[2],
        "status": row[3],
        "started_at": row[4],
    }


def _decode_features_version(features_json: str) -> str:
    try:
        payload = json.loads(features_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AuthorizationError("stored features_json is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthorizationError("stored features_json is not an object")
    version = payload.get("features_version")
    if version != FEATURES_SCHEMA_VERSION:
        raise AuthorizationError("unsupported features_version in stored window")
    return version


def query_windows(
    conn: sqlite3.Connection,
    run_id: str,
    device_id: str,
    *,
    high_water_start_seq: int,
    last_start_seq: int | None,
    page_size: int,
) -> tuple[list[WindowItem], bool]:
    """Return one page of windows plus whether another page exists.

    Ordering is start_seq DESC. The first page is bounded by
    high_water_start_seq (inclusive); later pages are bounded by
    last_start_seq (exclusive). fetch is page_size + 1 so a full extra row
    means there is a next page.
    """
    if last_start_seq is None:
        upper = high_water_start_seq
        sql = (
            "SELECT " + _WINDOW_COLS + " FROM feature_windows "
            "WHERE run_id = ? AND device_id = ? AND start_seq <= ? "
            "ORDER BY start_seq DESC LIMIT ?"
        )
        params: tuple = (run_id, device_id, upper, page_size + 1)
    else:
        sql = (
            "SELECT " + _WINDOW_COLS + " FROM feature_windows "
            "WHERE run_id = ? AND device_id = ? AND start_seq < ? "
            "ORDER BY start_seq DESC LIMIT ?"
        )
        params = (run_id, device_id, last_start_seq, page_size + 1)

    rows = conn.execute(sql, params).fetchall()
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    items: list[WindowItem] = []
    for start_seq, end_seq_exclusive, health_score, features_json in rows:
        version = _decode_features_version(features_json)
        payload = json.loads(features_json)
        items.append(
            WindowItem(
                start_seq=start_seq,
                end_seq_exclusive=end_seq_exclusive,
                health_score=health_score,
                features_version=version,
                scoring_version=payload.get("scoring_version"),
                rule_id=payload.get("rule_id"),
            )
        )
    return items, has_more


def query_alarms(
    conn: sqlite3.Connection,
    run_id: str,
    device_id: str,
    *,
    high_water_start_seq: int,
    high_water_rule_id: str,
    last_start_seq: int | None,
    last_rule_id: str | None,
    page_size: int,
) -> tuple[list[AlarmItem], bool]:
    """Return one page of alarms plus whether another page exists.

    Ordering is start_seq DESC, rule_id DESC. The first page is bounded by the
    high-water key (inclusive on both columns); later pages use the previous
    page's last key (exclusive on rule_id).
    """
    if last_start_seq is None:
        sql = (
            "SELECT " + _ALARM_COLS + " FROM alarms "
            "WHERE run_id = ? AND device_id = ? AND "
            "(start_seq < ? OR (start_seq = ? AND rule_id <= ?)) "
            "ORDER BY start_seq DESC, rule_id DESC LIMIT ?"
        )
        params = (
            run_id,
            device_id,
            high_water_start_seq,
            high_water_start_seq,
            high_water_rule_id,
            page_size + 1,
        )
    else:
        sql = (
            "SELECT " + _ALARM_COLS + " FROM alarms "
            "WHERE run_id = ? AND device_id = ? AND "
            "(start_seq < ? OR (start_seq = ? AND rule_id < ?)) "
            "ORDER BY start_seq DESC, rule_id DESC LIMIT ?"
        )
        params = (
            run_id,
            device_id,
            last_start_seq,
            last_start_seq,
            last_rule_id,
            page_size + 1,
        )

    rows = conn.execute(sql, params).fetchall()
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    items = [AlarmItem(rule_id=row[0], start_seq=row[1], level=row[2]) for row in rows]
    return items, has_more


def query_health(
    conn: sqlite3.Connection,
    run_id: str,
    device_id: str,
) -> HealthView | None:
    """Return the allow-listed health projection for the device.

    The device's active alarm state is read from alarm_states (one row per
    rule_id). Its state_json envelope is parsed and only the eight allow-listed
    fields are projected; the raw envelope and every audit field are dropped.
    The manifest_schema_version comes from runs.schema_version, never from a
    hard-coded constant.
    """
    identity = read_run_identity(conn, run_id)
    if identity is None:
        return None

    state_rows = conn.execute(
        "SELECT rule_id, state_json FROM alarm_states "
        "WHERE run_id = ? AND device_id = ?",
        (run_id, device_id),
    ).fetchall()
    if not state_rows:
        return None

    # Prefer an active alarm (active_level is not null); otherwise the first.
    chosen = state_rows[0]
    for rule_id, state_json in state_rows:
        try:
            payload = json.loads(state_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AuthorizationError("stored state_json is not valid JSON") from exc
        state = payload.get("state") if isinstance(payload, dict) else None
        if isinstance(state, dict) and state.get("active_level") is not None:
            chosen = (rule_id, state_json)
            break

    state_json = chosen[1]
    try:
        payload = json.loads(state_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AuthorizationError("stored state_json is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthorizationError("stored state_json is not an object")
    state_schema_version = payload.get("state_schema_version")
    if state_schema_version != STATE_SCHEMA_VERSION:
        raise AuthorizationError("unsupported state_schema_version in stored state")
    body = payload.get("state")
    if not isinstance(body, dict):
        raise AuthorizationError("stored state_json state member is not an object")

    projected = {name: body.get(name) for name in STATE_PROJECTION_FIELDS}
    return HealthView(
        manifest_schema_version=identity["schema_version"],
        state_schema_version=state_schema_version,
        state=projected,
    )


def window_high_water(conn, run_id, device_id):
    """Return the largest committed window start_seq, or None if none exist.

    This is the inclusive upper bound recorded on the first page's cursor so
    windows committed after the first page is read are never seen on later
    pages of the same stable scan.
    """
    row = conn.execute(
        "SELECT MAX(start_seq) FROM feature_windows WHERE run_id = ? AND device_id = ?",
        (run_id, device_id),
    ).fetchone()
    return row[0]


def alarm_high_water(conn, run_id, device_id):
    """Return the largest committed alarm key (start_seq, rule_id) or None.

    When several alarms share the maximum start_seq, the largest rule_id wins,
    matching the start_seq DESC, rule_id DESC ordering used by query_alarms.
    """
    row = conn.execute(
        "SELECT MAX(start_seq) FROM alarms WHERE run_id = ? AND device_id = ?",
        (run_id, device_id),
    ).fetchone()
    max_start = row[0]
    if max_start is None:
        return None
    row = conn.execute(
        "SELECT MAX(rule_id) FROM alarms "
        "WHERE run_id = ? AND device_id = ? AND start_seq = ?",
        (run_id, device_id, max_start),
    ).fetchone()
    return max_start, row[0]
