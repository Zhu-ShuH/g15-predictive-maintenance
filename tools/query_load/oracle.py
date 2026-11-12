"""Independent SQLite oracle for item-by-item response verification.

The oracle opens the committed store with its own sqlite3 connection and its own
SQL.  It does not import the repository write path and never reads
private_truth.  Each window / alarm / state item is looked up by its own key
(start_seq, or start_seq + rule_id), so page-by-page and cursor-driven responses
are both checked against the exact committed row rather than against whatever a
fresh first-page LIMIT would return.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _load_features_json(text: str) -> dict[str, object]:
    return json.loads(text)


def _load_state_json(text: str) -> dict[str, object]:
    return json.loads(text)


def _row(connection: sqlite3.Connection, sql: str, params: tuple) -> tuple | None:
    return connection.execute(sql, params).fetchone()


def verify_response(
    body: dict[str, object],
    db_path: Path,
    run_id: str,
    device_id: str,
    page_size: int,
    *,
    first_page: bool = False,
    first_page_streams: tuple[str, ...] = (),
) -> list[str]:
    """Check rows and page shape against the independent committed store.

    An omitted cursor requests that stream's first page, including replays
    of an already consumed stream. Callers must supply that request context.
    """
    problems: list[str] = []
    if body.get("run_id") != run_id or body.get("device_id") != device_id:
        problems.append("response run/device identity mismatch")
    for stream in ("windows", "alarms"):
        section = body.get(stream)
        if not isinstance(section, dict) or not isinstance(section.get("items"), list):
            return problems + [f"{stream}.items missing or malformed"]
        if len(section["items"]) > page_size:
            problems.append(f"{stream} exceeds requested page_size")
        if any(not isinstance(item, dict) for item in section["items"]):
            return problems + [f"{stream} item malformed"]
        if any(
            type(item.get("start_seq")) is not int
            or (stream == "alarms" and not isinstance(item.get("rule_id"), str))
            for item in section["items"]
        ):
            return problems + [f"{stream} item key malformed"]
        keys = [
            (
                (item["start_seq"],)
                if stream == "windows"
                else (item["start_seq"], item["rule_id"])
            )
            for item in section["items"]
        ]
        if len(keys) != len(set(keys)):
            problems.append(f"{stream} duplicate keys within page")
        if keys != sorted(keys, reverse=True):
            problems.append(f"{stream} keys out of descending order")
    if not isinstance(body.get("states"), list) or any(
        not isinstance(item, dict) for item in body["states"]
    ):
        return problems + ["states missing or malformed"]
    connection = sqlite3.connect(str(db_path))
    try:
        run_row = _row(
            connection,
            "SELECT schema_version, scoring_version, status FROM runs WHERE run_id = ?",
            (run_id,),
        )
        if run_row is None:
            return ["oracle: run row missing"]
        for stream, table, keys, order in (
            ("windows", "feature_windows", ("start_seq",), "start_seq DESC"),
            (
                "alarms",
                "alarms",
                ("start_seq", "rule_id"),
                "start_seq DESC, rule_id DESC",
            ),
        ):
            if not first_page and stream not in first_page_streams:
                continue
            expected = connection.execute(
                f"SELECT {', '.join(keys)} FROM {table} "
                f"WHERE run_id = ? AND device_id = ? ORDER BY {order} LIMIT ?",
                (run_id, device_id, page_size),
            ).fetchall()
            actual = [
                tuple(item.get(key) for key in keys) for item in body[stream]["items"]
            ]
            if actual != expected:
                problems.append(f"{stream} first-page keys/order mismatch")
        expected_states = connection.execute(
            "SELECT rule_id FROM alarm_states WHERE run_id = ? AND device_id = ? ORDER BY rule_id",
            (run_id, device_id),
        ).fetchall()
        actual_states = [(item.get("rule_id"),) for item in body["states"]]
        if sorted(actual_states, key=str) != sorted(expected_states, key=str):
            problems.append("states key coverage mismatch")
        if str(body.get("run_status")) != str(run_row[2]):
            problems.append("run_status mismatch")
        versions = body.get("versions")
        if not isinstance(versions, dict):
            problems.append("versions missing")
            return problems
        if str(versions.get("manifest_schema_version")) != str(run_row[0]):
            problems.append("manifest_schema_version mismatch")
        if str(versions.get("run_scoring_version")) != str(run_row[1]):
            problems.append("run_scoring_version mismatch")

        # Windows: look each returned item up by its own start_seq.
        items = body.get("windows", {}).get("items", [])
        if not isinstance(items, list):
            problems.append("windows.items missing")
        else:
            for position, item in enumerate(items):
                start_seq = item.get("start_seq")
                row = _row(
                    connection,
                    "SELECT end_seq_exclusive, health_score, features_json "
                    "FROM feature_windows WHERE run_id = ? AND device_id = ? "
                    "AND start_seq = ?",
                    (run_id, device_id, start_seq),
                )
                if row is None:
                    problems.append(
                        f"window[{position}] start_seq {start_seq} not committed"
                    )
                    continue
                end_seq_exclusive, health_score, features_json = row
                features = _load_features_json(features_json)
                if item.get("end_seq_exclusive") != end_seq_exclusive:
                    problems.append(f"window[{position}] end_seq_exclusive mismatch")
                if item.get("health_score") != health_score:
                    problems.append(f"window[{position}] health_score mismatch")
                for key in (
                    "features_version",
                    "sample_count",
                    "sample_rate_hz",
                    "scoring_version",
                    "rule_id",
                ):
                    if item.get(key) != features.get(key):
                        problems.append(f"window[{position}] {key} mismatch")

        # Alarms: look each returned item up by its own (start_seq, rule_id).
        alarms = body.get("alarms", {}).get("items", [])
        if not isinstance(alarms, list):
            problems.append("alarms.items missing")
        else:
            for position, item in enumerate(alarms):
                start_seq = item.get("start_seq")
                rule_id = item.get("rule_id")
                row = _row(
                    connection,
                    "SELECT level, reason, created_at FROM alarms "
                    "WHERE run_id = ? AND device_id = ? AND start_seq = ? "
                    "AND rule_id = ?",
                    (run_id, device_id, start_seq, rule_id),
                )
                if row is None:
                    problems.append(
                        f"alarm[{position}] ({start_seq}, {rule_id}) not committed"
                    )
                    continue
                level, reason, created_at = row
                if item.get("level") != level:
                    problems.append(f"alarm[{position}] level mismatch")
                if item.get("reason") != reason:
                    problems.append(f"alarm[{position}] reason mismatch")
                if item.get("created_at") != created_at:
                    problems.append(f"alarm[{position}] created_at mismatch")

        # States: one committed row per rule_id.
        states = body.get("states", [])
        if not isinstance(states, list):
            problems.append("states missing")
        else:
            for item in states:
                rule_id = item.get("rule_id")
                state_row = _row(
                    connection,
                    "SELECT state_json, last_end_seq_exclusive FROM alarm_states "
                    "WHERE run_id = ? AND device_id = ? AND rule_id = ?",
                    (run_id, device_id, rule_id),
                )
                if state_row is None:
                    problems.append(f"state {rule_id} not in committed store")
                    continue
                state_json, last_end_seq_exclusive = state_row
                committed = _load_state_json(state_json)["state"]
                got = item.get("state", {})
                if not isinstance(got, dict):
                    problems.append(f"state[{rule_id}] malformed state object")
                    continue
                for key in (
                    "active_level",
                    "consecutive_breaches",
                    "normal_windows",
                    "alarm_windows",
                    "last_window_start_seq",
                    "recovered_at_seq",
                    "scoring_version",
                    "rule_id",
                ):
                    if got.get(key) != committed.get(key):
                        problems.append(f"state[{rule_id}] {key} mismatch")
                if item.get("last_end_seq_exclusive") != last_end_seq_exclusive:
                    problems.append(f"state[{rule_id}] last_end_seq mismatch")
        return problems
    finally:
        connection.close()
