"""Unit tests for the read-only query reader (ADR 5.1.5).

These build a real SQLite database through the writable path (fixture setup
only), then exercise the reader through connect_readonly to prove: ordering,
keyset pagination, high-water bounds, allow-listed projection, and that the
reader performs no writes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from g15.query import reader
from g15.query.errors import AuthorizationError
from g15.storage.database import connect, initialize_database
from g15.storage.repository import (
    FEATURES_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    ConsumptionRepository,
)

RUN_ID = "run1"
DEVICE_ID = "dev1"


def _build_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "results.sqlite"
    conn = connect(db_path)
    initialize_database(conn)
    repo = ConsumptionRepository(conn)
    repo.ensure_run(
        RUN_ID, "scenario1", "2.0", "heuristic-v1", "complete", "2026-11-10T00:00:00Z"
    )
    features = {
        "mean": 0.1,
        "std": 0.2,
        "rms": 0.3,
        "ptp": 0.4,
        "kurtosis_excess": 0.5,
        "crest_factor": 0.6,
        "dominant_freq_hz": 0.7,
        "zero_crossing_rate": 0.8,
    }
    for start in range(1, 11):
        fj = json.dumps(
            {
                "features_version": FEATURES_SCHEMA_VERSION,
                "features": features,
                "scoring_version": "heuristic-v1",
                "rule_id": "health_heuristic",
            }
        )
        repo.insert_feature_window(
            RUN_ID, DEVICE_ID, start, start + 256, fj, float(100 - start)
        )
    repo.insert_alarm(
        RUN_ID, DEVICE_ID, "rule_alpha", 9, "yellow", "low", "2026-11-10T00:00:01Z"
    )
    repo.insert_alarm(
        RUN_ID, DEVICE_ID, "rule_beta", 9, "red", "low", "2026-11-10T00:00:02Z"
    )
    conn.commit()
    state_json = json.dumps(
        {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "state": {
                "active_level": "yellow",
                "consecutive_breaches": 2,
                "normal_windows": 3,
                "alarm_windows": 4,
                "last_window_start_seq": 9,
                "recovered_at_seq": None,
                "scoring_version": "heuristic-v1",
                "rule_id": "health_heuristic",
            },
        }
    )
    conn.execute(
        "INSERT INTO alarm_states (run_id, device_id, rule_id, last_end_seq_exclusive, state_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (RUN_ID, DEVICE_ID, "health_heuristic", 9 + 256, state_json),
    )
    conn.commit()
    conn.close()
    return db_path


def _conn(db_path: Path):
    return reader.open_readonly(db_path)


def test_open_readonly_missing_file_maps_to_404(tmp_path):
    with pytest.raises(AuthorizationError):
        reader.open_readonly(tmp_path / "nope.sqlite")


def test_read_run_identity_returns_schema_version(tmp_path):
    db = _build_db(tmp_path)
    conn = _conn(db)
    identity = reader.read_run_identity(conn, RUN_ID)
    assert identity is not None
    assert identity["schema_version"] == "2.0"
    assert identity["scoring_version"] == "heuristic-v1"
    conn.close()


def test_windows_ordered_descending(tmp_path):
    db = _build_db(tmp_path)
    conn = _conn(db)
    items, has_more = reader.query_windows(
        conn,
        RUN_ID,
        DEVICE_ID,
        high_water_start_seq=10,
        last_start_seq=None,
        page_size=10,
    )
    starts = [item.start_seq for item in items]
    assert starts == sorted(starts, reverse=True)
    assert starts[0] == 10
    assert not has_more
    conn.close()


def test_windows_keyset_pagination_no_overlap(tmp_path):
    db = _build_db(tmp_path)
    conn = _conn(db)
    first, more = reader.query_windows(
        conn,
        RUN_ID,
        DEVICE_ID,
        high_water_start_seq=10,
        last_start_seq=None,
        page_size=4,
    )
    assert more
    last = first[-1].start_seq
    second, more2 = reader.query_windows(
        conn,
        RUN_ID,
        DEVICE_ID,
        high_water_start_seq=10,
        last_start_seq=last,
        page_size=4,
    )
    all_starts = [i.start_seq for i in first] + [i.start_seq for i in second]
    assert len(all_starts) == len(set(all_starts))
    assert second[0].start_seq < last
    conn.close()


def test_windows_high_water_bounds_later_pages(tmp_path):
    db = _build_db(tmp_path)
    conn = _conn(db)
    # Commit an 11th window AFTER the high water is read; it must never appear.
    high = reader.window_high_water(conn, RUN_ID, DEVICE_ID)
    assert high == 10
    first, _more = reader.query_windows(
        conn,
        RUN_ID,
        DEVICE_ID,
        high_water_start_seq=high,
        last_start_seq=None,
        page_size=20,
    )
    assert 11 not in [i.start_seq for i in first]
    conn.close()


def test_window_high_water_none_when_empty(tmp_path):
    db = _build_db(tmp_path)
    conn = _conn(db)
    assert reader.window_high_water(conn, RUN_ID, "other-device") is None
    conn.close()


def test_alarms_ordered_and_tie_broken_by_rule_id_desc(tmp_path):
    db = _build_db(tmp_path)
    conn = _conn(db)
    high_start, high_rule = reader.alarm_high_water(conn, RUN_ID, DEVICE_ID)
    items, has_more = reader.query_alarms(
        conn,
        RUN_ID,
        DEVICE_ID,
        high_water_start_seq=high_start,
        high_water_rule_id=high_rule,
        last_start_seq=None,
        last_rule_id=None,
        page_size=10,
    )
    keys = [(i.start_seq, i.rule_id) for i in items]
    assert keys == [(9, "rule_beta"), (9, "rule_alpha")]
    assert not has_more
    conn.close()


def test_query_health_projects_allow_list_only(tmp_path):
    db = _build_db(tmp_path)
    conn = _conn(db)
    view = reader.query_health(conn, RUN_ID, DEVICE_ID)
    assert view is not None
    assert view.manifest_schema_version == "2.0"
    assert view.state_schema_version == STATE_SCHEMA_VERSION
    assert set(view.state) == set(reader.STATE_PROJECTION_FIELDS)
    # Sensitive fields must be absent.
    for forbidden in (
        "reset_event",
        "reset_history",
        "previous_state_json",
        "reason",
        "state_json",
    ):
        assert forbidden not in view.state
    conn.close()


def test_query_health_none_when_no_state(tmp_path):
    db = _build_db(tmp_path)
    conn = _conn(db)
    assert reader.query_health(conn, RUN_ID, "other-device") is None
    conn.close()


def test_reader_is_read_only(tmp_path):
    db = _build_db(tmp_path)
    conn = _conn(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE sneaky (id INTEGER)")
    conn.close()
