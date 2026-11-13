"""Phone evidence is a complete public, committed, single-transaction view."""

import json
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from g15.analysis.alarms import AlarmState
from g15.live import notification_snapshot as notifications
from g15.live import reporting
from g15.live.schema import SCENARIO_ID
from g15.storage.database import initialize_database
from g15.storage.repository import StorageError, encode_state_json


def _alarm(connection, rule, start, level, driver="rms"):
    connection.execute(
        "INSERT INTO alarms VALUES(?,?,?,?,?,?,?)",
        (
            "live-run",
            "pump-1",
            rule,
            start,
            level,
            f"threshold; drivers: {driver}=2.0 (risk 0.8)",
            "2026-11-13T00:00:00Z",
        ),
    )


@pytest.fixture(name="public_run")
def _public_run(tmp_path):
    root = tmp_path / "runs"
    directory = root / "live-run"
    database = directory / "artifacts" / "results.sqlite"
    database.parent.mkdir(parents=True)
    live = {
        "schema_version": "1",
        "session_id": "live-session",
        "run_id": "live-run",
        "device_id": "pump-1",
        "scenario_id": SCENARIO_ID,
        "source_kind": "wall_clock_simulator",
        "sample_rate_hz": 160,
        "window_points": 16,
        "interval_seconds": 0.1,
        "started_at_utc": "2026-11-13T00:00:00Z",
        "updated_at_utc": "2026-11-13T00:01:00Z",
        "status": "stopped",
        "captured_samples": 4800,
        "processed_samples": 4800,
        "windows_written": 300,
        "alarms_written": 251,
        "scoring_version": "live-score",
        "rule_id": "health-red",
        "error": None,
    }
    (directory / "live-session.json").write_text(json.dumps(live), encoding="utf-8")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        initialize_database(connection)
        connection.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?)",
            (
                "live-run",
                SCENARIO_ID,
                "1",
                "live-score",
                "streaming",
                live["started_at_utc"],
            ),
        )
        connection.executemany(
            "INSERT INTO feature_windows VALUES(?,?,?,?,?,?)",
            [
                ("live-run", "pump-1", i * 16, (i + 1) * 16, "{}", 25.0)
                for i in range(300)
            ],
        )
        for index in range(121):
            _alarm(
                connection,
                "health-red",
                index * 16,
                "red",
                "rms" if index == 120 else "ptp",
            )
        for index in range(130):
            _alarm(connection, "health-yellow", index * 16, "yellow", "std")
        for rule, level in (("health-red", "red"), ("health-yellow", "yellow")):
            state = AlarmState(
                active_level=level,
                consecutive_breaches=1,
                alarm_windows=1,
                last_window_start_seq=4784,
                scoring_version="live-score",
                rule_id=rule,
            )
            connection.execute(
                "INSERT INTO alarm_states VALUES(?,?,?,?,?)",
                ("live-run", "pump-1", rule, 4800, encode_state_json(state)),
            )
    connection.close()
    return SimpleNamespace(root=root, directory=directory, database=database, live=live)


def _read(run, **options):
    return notifications.read_notification_snapshot(
        run.root, "live-run", "pump-1", **options
    )


def test_red_count_covers_full_run_and_advice_uses_latest_actual_rule_sources(
    public_run,
):
    snapshot = _read(public_run)
    assert snapshot.complete is True
    assert snapshot.red_count == 121
    assert snapshot.red_count != snapshot.report_json["feature_windows"] == 300
    assert snapshot.red_count != snapshot.report_json["alarm_count"] == 251
    assert snapshot.last_red_id == _read(public_run).last_red_id
    assert any("RMS" in item for item in snapshot.advice)
    assert any("振动波动" in item for item in snapshot.advice)
    assert all("峰峰值" not in item for item in snapshot.advice)
    assert (
        "Committed windows: 300; alarms: 251; processed samples: 4800."
        in snapshot.report_markdown
    )
    assert snapshot.report_json["count_basis"] == "single_readonly_sqlite_transaction"
    assert snapshot.report_json["verifiable"] is False
    for name in (
        "passed",
        "detection_rate",
        "false_alarm_count",
        "fault_events",
        "detected_events",
        "missed_events",
        "redundant_alarm_count",
        "unmatched_alarm_count",
        "dropped_points",
    ):
        assert snapshot.report_json[name] is None


def test_report_red_count_and_advice_share_one_readonly_transaction(
    public_run, monkeypatch
):
    original_connect = reporting.connect_readonly
    connections, statements = [], []
    committed = False

    def connect(database):
        connection = original_connect(database)
        connections.append(connection)

        def trace(statement):
            nonlocal committed
            statements.append(statement)
            if "COALESCE(SUM" in statement and not committed:
                committed = True
                with sqlite3.connect(public_run.database) as writer:
                    writer.execute(
                        "INSERT INTO feature_windows VALUES(?,?,?,?,?,?)",
                        ("live-run", "pump-1", 9999, 10015, "{}", 5.0),
                    )
                    _alarm(writer, "health-red", 9999, "red", "ptp")

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(reporting, "connect_readonly", connect)
    snapshot = _read(public_run)
    assert committed and len(connections) == 1
    assert statements.count("BEGIN") == statements.count("COMMIT") == 1
    assert snapshot.red_count == 121
    assert snapshot.report_json["feature_windows"] == 300
    assert snapshot.report_json["alarm_count"] == 251
    assert "Committed windows: 300; alarms: 251" in snapshot.report_markdown
    assert any("RMS" in item for item in snapshot.advice)
    assert all("峰峰值" not in item for item in snapshot.advice)
    newer = _read(public_run)
    assert newer.red_count == 122
    assert newer.last_red_id != snapshot.last_red_id


def test_rolled_back_alarm_is_neither_counted_nor_used_as_advice(public_run):
    before = _read(public_run)
    writer = sqlite3.connect(public_run.database)
    try:
        writer.execute("BEGIN")
        _alarm(writer, "health-red", 9999, "red", "ptp")
        during = _read(public_run)
        writer.rollback()
    finally:
        writer.close()
    after = _read(public_run)
    assert before.red_count == during.red_count == after.red_count == 121
    assert before.last_red_id == during.last_red_id == after.last_red_id
    assert before.advice == during.advice == after.advice


def test_active_rule_uses_its_latest_red_even_with_a_newer_yellow(public_run):
    with sqlite3.connect(public_run.database) as connection:
        _alarm(connection, "health-red", 5000, "yellow", "ptp")
    snapshot = _read(public_run)
    assert snapshot.red_count == 121
    assert any("RMS" in item for item in snapshot.advice)
    assert all("峰峰值" not in item for item in snapshot.advice)


def test_red_identity_includes_rule_at_same_sequence_and_scopes_exact_pair(public_run):
    before = _read(public_run)
    with sqlite3.connect(public_run.database) as connection:
        _alarm(connection, "zz-other-rule", 1920, "red")
        connection.execute(
            "INSERT INTO alarms VALUES(?,?,?,?,?,?,?)",
            (
                "live-run",
                "foreign-device",
                "health-red",
                99999,
                "red",
                "foreign",
                "now",
            ),
        )
    after = _read(public_run)
    assert after.red_count == 122
    assert after.last_red_id != before.last_red_id
    assert _read(public_run).last_red_id == after.last_red_id


def test_empty_red_history_and_empty_health_remain_explicitly_unverifiable(public_run):
    with sqlite3.connect(public_run.database) as connection:
        connection.execute("DELETE FROM alarms")
        connection.execute("DELETE FROM alarm_states")
        connection.execute("DELETE FROM feature_windows")
    snapshot = _read(public_run, live_status="stopped")
    assert snapshot.red_count == 0 and snapshot.last_red_id is None
    assert snapshot.advice == ()
    assert snapshot.report_json["health_score_mean"] is None
    assert snapshot.report_json["health_summary"] == {
        "count": 0,
        "minimum": None,
        "maximum": None,
        "mean": None,
    }
    assert snapshot.report_json["verifiable"] is False
    assert "minimum: None; maximum: None; mean: None." in snapshot.report_markdown


@pytest.mark.parametrize(
    "damage",
    [
        "descriptor",
        "database",
        "state_identity",
        "state_json",
        "state_duplicate",
        "state_level",
        "state_negative",
        "position",
    ],
)
def test_invalid_identity_or_source_data_fails_closed(public_run, damage):
    if damage == "descriptor":
        public_run.live["device_id"] = "foreign-device"
        (public_run.directory / "live-session.json").write_text(
            json.dumps(public_run.live), encoding="utf-8"
        )
    else:
        with sqlite3.connect(public_run.database) as connection:
            if damage == "database":
                connection.execute("UPDATE runs SET scoring_version='foreign-score'")
            elif damage == "position":
                _alarm(connection, "health-red", -1, "red")
                connection.execute(
                    "DELETE FROM alarms WHERE start_seq>=0 AND level='red'"
                )
            else:
                text = connection.execute(
                    "SELECT state_json FROM alarm_states WHERE rule_id='health-red'"
                ).fetchone()[0]
                value = json.loads(text)
                if damage == "state_identity":
                    value["state"]["rule_id"] = "foreign-rule"
                elif damage == "state_level":
                    value["state"]["active_level"] = "blue"
                elif damage == "state_negative":
                    value["state"]["alarm_windows"] = -1
                text = json.dumps(value)
                if damage == "state_json":
                    text = "invalid"
                elif damage == "state_duplicate":
                    text = text.replace(
                        '"active_level": "red"',
                        '"active_level": "yellow", "active_level": "red"',
                    )
                connection.execute(
                    "UPDATE alarm_states SET state_json=? WHERE rule_id='health-red'",
                    (text,),
                )
    with pytest.raises((ValueError, StorageError)):
        _read(public_run)


def test_source_overflow_is_rejected_instead_of_silently_truncated(public_run):
    with sqlite3.connect(public_run.database) as connection:
        for index in range(101):
            _alarm(connection, f"extra-{index}", index, "red")
    with pytest.raises(ValueError, match="incomplete"):
        _read(public_run)


def test_corrupt_database_never_produces_a_notification_snapshot(public_run):
    public_run.database.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        _read(public_run)


@pytest.mark.parametrize("target", ["run", "descriptor", "artifacts", "database"])
def test_reparse_resources_are_rejected_before_database_open(
    public_run, monkeypatch, target
):
    selected = {
        "run": public_run.directory,
        "descriptor": public_run.directory / "live-session.json",
        "artifacts": public_run.database.parent,
        "database": public_run.database,
    }[target]
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == selected:
            return SimpleNamespace(
                st_mode=info.st_mode | stat.S_IFLNK,
                st_file_attributes=getattr(info, "st_file_attributes", 0) | 0x400,
            )
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(
        reporting,
        "connect_readonly",
        lambda _path: pytest.fail("opened rejected database"),
    )
    with pytest.raises(ValueError, match="reparse"):
        _read(public_run)


def test_snapshot_reads_no_truth_or_persisted_report_files(public_run, monkeypatch):
    truth = public_run.directory / "private_truth"
    reports = public_run.directory / "reports"
    truth.mkdir()
    reports.mkdir()
    (truth / "faults.json").write_text("private-truth-sentinel", encoding="utf-8")
    (reports / "summary.json").write_text("invalid persisted report", encoding="utf-8")
    (reports / "summary.md").write_text("private stale markdown", encoding="utf-8")
    original_open = os.open
    original_path_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert not {"private_truth", "reports"}.intersection(Path(path).parts)
        return original_open(path, *args, **kwargs)

    def guarded_path_open(path, *args, **kwargs):
        assert not {"private_truth", "reports"}.intersection(path.parts)
        return original_path_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    snapshot = _read(public_run)
    assert snapshot.red_count == 121
    assert "private" not in snapshot.report_markdown


def test_live_report_formatter_keeps_existing_public_format(public_run, monkeypatch):
    monkeypatch.setattr(reporting, "utc_now", lambda: "2026-11-13T01:00:00Z")
    snapshot = _read(public_run)
    assert (
        json.loads(
            reporting.render_live_report(public_run.root, "live-run", "pump-1", "json")
        )
        == snapshot.report_json
    )
    assert (
        reporting.render_live_report(
            public_run.root, "live-run", "pump-1", "md"
        ).decode()
        == snapshot.report_markdown
    )
    assert set(snapshot.report_json) == {
        "report_schema_version",
        "run_id",
        "device_id",
        "scenario_id",
        "scenario_kind",
        "source_kind",
        "generated_at_utc",
        "verifiable",
        "passed",
        "detection_rate",
        "false_alarm_count",
        "fault_events",
        "detected_events",
        "missed_events",
        "redundant_alarm_count",
        "unmatched_alarm_count",
        "feature_windows",
        "alarm_count",
        "windows_written",
        "alarms_written",
        "processed_samples",
        "health_score_min",
        "health_score_max",
        "health_score_mean",
        "health_summary",
        "live_status",
        "count_basis",
        "dropped_points",
        "verdict_reasons",
    }
