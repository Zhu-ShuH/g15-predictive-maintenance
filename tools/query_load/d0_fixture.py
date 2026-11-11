"""Deterministic D0 fixture: 10 devices x 100 committed feature windows.

Gate 0 (Issue #136) needs a small, correct, fully committed SQLite store per
device so the capacity harness can drive the real read-only query path end to
end.  The data is synthetic and carries no private truth; it only has to be
internally consistent with the stored contract so the oracle can verify each
response item-by-item.

The store mirrors the fixture used by tests/integration/test_query_service.py:
a hand-written manifest.json (schema 2.0, status complete) plus a real SQLite
database created through connect()/initialize_database()/transaction().
"""

from __future__ import annotations

import json
from pathlib import Path

from g15.storage.database import connect, initialize_database, transaction

D0_DEVICE_COUNT = 10
D0_WINDOWS_PER_DEVICE = 100
D0_WINDOW_SPAN = 10

D0_SCORING_VERSION = "score-d0-1"
D0_RULE_ID = "health-d0"
D0_MANIFEST_SCHEMA_VERSION = "2.0"
D0_POLICY_VERSION = "policy-d0-1"
D0_SAMPLE_RATE_HZ = 100
D0_ACTOR_REF = "actor-d0"
D0_PERMISSION_DOMAIN = "plant-d0"
D0_SUBJECT = "subject-d0"


def device_run_id(index: int) -> str:
    """Return the run_id for the index-th D0 device."""
    return f"run-d0-{index:03d}"


def device_id(index: int) -> str:
    """Return the device_id for the index-th D0 device."""
    return f"device-d0-{index:03d}"


def window_start_seq(window_index: int) -> int:
    """Return the start_seq of the window_index-th window."""
    return window_index * D0_WINDOW_SPAN


def build_policy_yaml() -> str:
    """Return an authorization policy granting one subject all ten devices."""
    allow = "".join(
        "      - run_id: {run}\n        device_id: {dev}\n".format(
            run=device_run_id(i), dev=device_id(i)
        )
        for i in range(D0_DEVICE_COUNT)
    )
    return (
        'authz_schema_version: "1"\n'
        f'policy_version: "{D0_POLICY_VERSION}"\n'
        "subjects:\n"
        f"  {D0_SUBJECT}:\n"
        f"    actor_ref: {D0_ACTOR_REF}\n"
        f"    permission_domain: {D0_PERMISSION_DOMAIN}\n"
        "    allow:\n"
        f"{allow}"
    )


def _manifest(index: int, total_points: int) -> dict[str, object]:
    return {
        "schema_version": D0_MANIFEST_SCHEMA_VERSION,
        "run_id": device_run_id(index),
        "scenario_id": "capacity-d0",
        "device_id": device_id(index),
        "seed": 7,
        "sample_rate_hz": D0_SAMPLE_RATE_HZ,
        "start_time_utc": "2026-11-11T00:00:00Z",
        "total_points": total_points,
        "units": {"vibration": "g", "temperature": "degC"},
        "status": "complete",
        "parts": [
            {
                "part_id": 0,
                "path": "raw/part-000000.npz",
                "rows": total_points,
                "sha256": "0" * 64,
            }
        ],
    }


def _features_json(window_index: int) -> str:
    start_seq = window_start_seq(window_index)
    return json.dumps(
        {
            "features_version": "1",
            "features": {},
            "sample_count": D0_WINDOW_SPAN,
            "sample_rate_hz": D0_SAMPLE_RATE_HZ,
            "window_start_seq": start_seq,
            "scoring_version": D0_SCORING_VERSION,
            "rule_id": D0_RULE_ID,
            "health_explain": "synthetic explanation",
        },
        sort_keys=True,
    )


def _state_json() -> str:
    return json.dumps(
        {
            "state_schema_version": "1",
            "state": {
                "active_level": "normal",
                "consecutive_breaches": 0,
                "normal_windows": D0_WINDOWS_PER_DEVICE,
                "alarm_windows": 0,
                "last_window_start_seq": window_start_seq(D0_WINDOWS_PER_DEVICE - 1),
                "recovered_at_seq": None,
                "scoring_version": D0_SCORING_VERSION,
                "rule_id": D0_RULE_ID,
            },
            "reset_event": {
                "reason": "operator-private-reset-reason",
                "previous_state_json": "private-reset-envelope",
            },
        },
        sort_keys=True,
    )


def build_one_device(runs_root: Path, index: int) -> Path:
    """Build one committed D0 device store under runs_root.

    Returns the database path so callers can open an oracle connection.
    """
    run_id = device_run_id(index)
    run_dir = runs_root / run_id
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "artifacts").mkdir()
    total_points = D0_WINDOWS_PER_DEVICE * D0_WINDOW_SPAN
    (run_dir / "manifest.json").write_text(
        json.dumps(_manifest(index, total_points), sort_keys=True), encoding="utf-8"
    )
    database_path = run_dir / "artifacts" / "results.sqlite"
    connection = connect(database_path)
    initialize_database(connection)
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO runs
              (run_id, scenario_id, schema_version, scoring_version,
               status, started_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "capacity-d0",
                D0_MANIFEST_SCHEMA_VERSION,
                D0_SCORING_VERSION,
                "complete",
                "2026-11-11T00:00:00Z",
            ),
        )
        for window_index in range(D0_WINDOWS_PER_DEVICE):
            start_seq = window_start_seq(window_index)
            connection.execute(
                """
                INSERT INTO feature_windows
                  (run_id, device_id, start_seq, end_seq_exclusive,
                   features_json, health_score)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    device_id(index),
                    start_seq,
                    start_seq + D0_WINDOW_SPAN,
                    _features_json(window_index),
                    100.0 - float(window_index % 10) * 5.0,
                ),
            )
        connection.execute(
            """
            INSERT INTO alarms
              (run_id, device_id, rule_id, start_seq, level, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                device_id(index),
                D0_RULE_ID,
                window_start_seq(D0_WINDOWS_PER_DEVICE - 1),
                "yellow",
                "synthetic threshold",
                "2026-11-11T00:00:01Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO alarm_states
              (run_id, device_id, rule_id, last_end_seq_exclusive, state_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                device_id(index),
                D0_RULE_ID,
                D0_WINDOWS_PER_DEVICE * D0_WINDOW_SPAN,
                _state_json(),
            ),
        )
    connection.close()
    return database_path


def build_d0_runs(runs_root: Path) -> list[Path]:
    """Build every D0 device store under runs_root and return database paths."""
    runs_root.mkdir(parents=True, exist_ok=True)
    return [build_one_device(runs_root, i) for i in range(D0_DEVICE_COUNT)]
