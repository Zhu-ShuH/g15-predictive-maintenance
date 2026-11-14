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
import math
import sqlite3
from pathlib import Path

_STATE_FIELDS = {
    "active_level": "nullable_text",
    "consecutive_breaches": "int",
    "normal_windows": "int",
    "alarm_windows": "int",
    "last_window_start_seq": "nullable_int",
    "recovered_at_seq": "nullable_int",
    "scoring_version": "nullable_text",
    "rule_id": "nullable_text",
}
_WINDOW_FIELDS = {
    "start_seq": "int",
    "end_seq_exclusive": "int",
    "health_score": "number",
    "features_version": "text",
    "sample_count": "int",
    "sample_rate_hz": "int",
    "scoring_version": "text",
    "rule_id": "text",
}
_ALARM_FIELDS = {
    "start_seq": "int",
    "rule_id": "text",
    "level": "text",
    "reason": "text",
    "created_at": "text",
}


def _matches_type(value: object, kind: str) -> bool:
    if kind.startswith("nullable_"):
        return value is None or _matches_type(value, kind.removeprefix("nullable_"))
    if kind == "number":
        return type(value) in (int, float) and math.isfinite(value)
    if kind == "texts":
        return isinstance(value, list) and all(type(item) is str for item in value)
    return (
        type(value)
        is {"int": int, "text": str, "object": dict, "list": list, "bool": bool}[kind]
    )


def _fields(
    value: object, spec: dict[str, str], label: str, problems: list[str]
) -> bool:
    if not isinstance(value, dict):
        problems.append(
            f"{label} malformed state object"
            if label.endswith(".state")
            else f"{label} missing or malformed object"
        )
        return False
    if set(value) != set(spec):
        problems.append(f"{label} field coverage mismatch")
    for key, kind in spec.items():
        if key in value and not _matches_type(value[key], kind):
            problems.append(f"{label}.{key} invalid JSON type")
    return True


def _schema_problems(body: dict[str, object], *, streams: str | None) -> list[str]:
    """Enforce the public projection at every level before comparing row values.

    Exact object fields also exclude arbitrary nested reset/truth payloads. JSON
    booleans and floats cannot stand in for integer counters; finite integer or
    float encodings are both valid for the health score.
    """
    problems: list[str] = []
    _fields(
        body,
        {
            "query_contract_version": "text",
            "run_id": "text",
            "device_id": "text",
            "request_id": "text",
            "trace_id": "text",
            "run_status": "text",
            "versions": "object",
            "committed_high_water": "object",
            "windows": "object",
            "alarms": "object",
            "states": "list",
        },
        "response",
        problems,
    )
    _fields(
        body.get("versions"),
        {
            "manifest_schema_version": "text",
            "run_scoring_version": "text",
            "features_versions": "texts",
            "state_schema_versions": "texts",
        },
        "versions",
        problems,
    )
    high = body.get("committed_high_water")
    if _fields(
        high,
        {
            "window_start_seq": "nullable_int",
            "alarm": "nullable_object",
            "state_last_end_seq_exclusive": "nullable_int",
        },
        "committed_high_water",
        problems,
    ) and isinstance(high.get("alarm"), dict):
        _fields(
            high["alarm"],
            {"start_seq": "int", "rule_id": "text"},
            "committed_high_water.alarm",
            problems,
        )
    for stream, item_spec in (("windows", _WINDOW_FIELDS), ("alarms", _ALARM_FIELDS)):
        section = body.get(stream)
        spec = {
            "sort": "text",
            "page_size": "int",
            "items": "list",
            "next_cursor": "nullable_text",
        }
        spec["high_water_start_seq" if stream == "windows" else "high_water"] = (
            "nullable_int" if stream == "windows" else "nullable_object"
        )
        if streams is not None:
            spec["omitted"] = "bool"
        if not _fields(section, spec, stream, problems):
            continue
        if stream == "alarms" and isinstance(section.get("high_water"), dict):
            _fields(
                section["high_water"],
                {"start_seq": "int", "rule_id": "text"},
                "alarms.high_water",
                problems,
            )
        if isinstance(section.get("items"), list):
            for index, item in enumerate(section["items"]):
                _fields(item, item_spec, f"{stream}[{index}]", problems)
    if isinstance(body.get("states"), list):
        for index, item in enumerate(body["states"]):
            if _fields(
                item,
                {
                    "rule_id": "text",
                    "last_end_seq_exclusive": "int",
                    "state_schema_version": "text",
                    "state": "object",
                },
                f"states[{index}]",
                problems,
            ):
                _fields(
                    item.get("state"), _STATE_FIELDS, f"states[{index}].state", problems
                )
    return problems


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
    streams: str | None = None,
) -> list[str]:
    """Check projection and values against SQLite and the caller's request.

    Version and omission are derived only from ``streams`` supplied by the
    caller. A response cannot opt itself out of item, state or schema checks.
    """
    if streams not in (None, "both", "windows", "alarms"):
        return ["invalid requested streams"]
    selected = ("windows", "alarms") if streams in (None, "both") else (streams,)
    problems = _schema_problems(body, streams=streams)
    if problems:
        return problems
    if body.get("query_contract_version") != ("1" if streams is None else "2"):
        problems.append("query_contract_version mismatch")
    if body.get("run_id") != run_id or body.get("device_id") != device_id:
        problems.append("response run/device identity mismatch")
    for stream in ("windows", "alarms"):
        section = body.get(stream)
        expected_sort = (
            "start_seq_desc" if stream == "windows" else "start_seq_desc_rule_id_desc"
        )
        if section["sort"] != expected_sort:
            problems.append(f"{stream} sort mismatch")
        if section["page_size"] != page_size:
            problems.append(f"{stream} page_size differs from request")
        if streams is not None and section["omitted"] is not (stream not in selected):
            problems.append(f"{stream} omitted differs from request")
        if stream not in selected:
            high_key = "high_water_start_seq" if stream == "windows" else "high_water"
            if (
                section["items"]
                or section["next_cursor"] is not None
                or section[high_key] is not None
            ):
                problems.append(
                    f"{stream} omitted projection must be empty with null cursor/high-water"
                )
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
            if stream not in selected or (
                not first_page and stream not in first_page_streams
            ):
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
        window_high = (
            _row(
                connection,
                "SELECT MAX(start_seq) FROM feature_windows WHERE run_id = ? AND device_id = ?",
                (run_id, device_id),
            )[0]
            if "windows" in selected
            else None
        )
        alarm_row = (
            _row(
                connection,
                "SELECT start_seq, rule_id FROM alarms WHERE run_id = ? AND device_id = ? "
                "ORDER BY start_seq DESC, rule_id DESC LIMIT 1",
                (run_id, device_id),
            )
            if "alarms" in selected
            else None
        )
        alarm_high = (
            {"start_seq": alarm_row[0], "rule_id": alarm_row[1]} if alarm_row else None
        )
        state_high = _row(
            connection,
            "SELECT MAX(last_end_seq_exclusive) FROM alarm_states WHERE run_id = ? AND device_id = ?",
            (run_id, device_id),
        )[0]
        expected_high = {
            "window_start_seq": window_high,
            "alarm": alarm_high,
            "state_last_end_seq_exclusive": state_high,
        }
        if body["committed_high_water"] != expected_high:
            problems.append(
                "committed_high_water differs from selected committed store"
            )
        if body["windows"]["high_water_start_seq"] != window_high:
            problems.append("windows high-water differs from selected committed store")
        if body["alarms"]["high_water"] != alarm_high:
            problems.append("alarms high-water differs from selected committed store")
        expected_states = connection.execute(
            "SELECT rule_id FROM alarm_states WHERE run_id = ? AND device_id = ? ORDER BY rule_id",
            (run_id, device_id),
        ).fetchall()
        actual_states = [(item.get("rule_id"),) for item in body["states"]]
        if sorted(actual_states, key=str) != sorted(expected_states, key=str):
            problems.append("states key coverage mismatch")
        if body.get("run_status") != run_row[2]:
            problems.append("run_status mismatch")
        versions = body.get("versions")
        if not isinstance(versions, dict):
            problems.append("versions missing")
            return problems
        if versions.get("manifest_schema_version") != run_row[0]:
            problems.append("manifest_schema_version mismatch")
        if versions.get("run_scoring_version") != run_row[1]:
            problems.append("run_scoring_version mismatch")

        feature_versions: set[str] = set()
        state_versions: set[str] = set()

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
                feature_versions.add(features["features_version"])
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
                envelope = _load_state_json(state_json)
                state_versions.add(envelope["state_schema_version"])
                if item.get("state_schema_version") != envelope["state_schema_version"]:
                    problems.append(f"state[{rule_id}] state_schema_version mismatch")
                committed = envelope["state"]
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
        if versions.get("features_versions") != sorted(feature_versions):
            problems.append("features_versions mismatch")
        if versions.get("state_schema_versions") != sorted(state_versions):
            problems.append("state_schema_versions mismatch")
        return problems
    finally:
        connection.close()
