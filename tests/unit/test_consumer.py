"""Consumer tests: the acceptance criteria of Issue #8 (owner C2).

The four properties ``docs/interface.md`` section 8 asks for are covered here:

* features, scores, alarms and the chunk marker share **one transaction**;
* consuming the same run twice does **not** duplicate rows;
* an interrupted consumption can be **replayed**;
* the consumer **refuses** what it cannot trust instead of guessing.

The fixtures build a real run directory with C4's producer, so the tests
exercise the same NPZ + manifest contract the rest of the project uses rather
than a hand-rolled fake.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from g15.analysis.alarms import AlarmState
from g15.config import ConsumerConfig, load_consumer_config
from g15.pipeline.consumer import ConsumptionError, consume_run
from g15.pipeline.producer import produce_run
from g15.storage.database import connect, initialize_database
from g15.storage.repository import (
    ConsumptionRepository,
    StateCompatibilityError,
    decode_state_json,
    encode_state_json,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _consumer_config(tmp_path: Path, **overrides: object) -> ConsumerConfig:
    """Build a consumer configuration with a small window.

    The rules file is referenced by an absolute path because the loader
    resolves a relative ``health_rules_file`` against the repository root of
    the *configuration file*, and this fixture lives in a temporary
    directory.
    """
    rules_path = REPO_ROOT / "configs" / "health_rules.yaml"
    document = {
        "schema_version": "1.0",
        "window_points": 64,
        "incomplete_window": "drop",
        "health_rules_file": str(rules_path),
    }
    document.update(overrides)
    path = tmp_path / "consumer.yaml"
    path.write_text(
        "\n".join(f"{key}: {json.dumps(value)}" for key, value in document.items()),
        encoding="utf-8",
    )
    return load_consumer_config(path)


def _make_run(tmp_path: Path, points: int = 512, chunk_points: int = 128) -> Path:
    """Produce a real run directory with the project's own producer."""
    config = {
        "schema_version": "1.0",
        "scenario": {
            "id": "c2_consumer_test",
            "seed": 42,
            "start_time_utc": "2026-10-14T00:00:00Z",
            "device_id": "DEV-C2",
            "sample_rate_hz": 100,
            "total_points": points,
            "chunk_points": chunk_points,
        },
        "signals": {
            "vibration": {
                "baseline": 0.5,
                "degradation_rate_per_sample": 0.0,
                "noise_std": 0.01,
                "unit": "g",
            },
            "temperature": {
                "baseline": 35.0,
                "degradation_rate_per_sample": 0.0,
                "noise_std": 0.01,
                "unit": "degC",
            },
        },
        "fault_generation": {
            "mode": "manual",
            "stochastic_probability_per_window": 0.0,
            "events": [],
        },
    }
    from g15.config import load_config

    scenario = load_config(_write_scenario(tmp_path, config))
    # ``produce_run`` takes the *output root* and creates a run directory
    # inside it; it returns the path of that directory.
    return produce_run(scenario, tmp_path)


def _write_scenario(tmp_path: Path, document: dict[str, object]) -> Path:
    """Write a scenario configuration file and return its path."""
    import yaml

    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _counts(database_path: Path) -> tuple[int, int, int]:
    """Return the row counts of the three result tables."""
    connection = sqlite3.connect(str(database_path))
    try:
        windows = connection.execute("SELECT COUNT(*) FROM feature_windows").fetchone()[
            0
        ]
        alarms = connection.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]
        chunks = connection.execute("SELECT COUNT(*) FROM processed_chunks").fetchone()[
            0
        ]
    finally:
        connection.close()
    return int(windows), int(alarms), int(chunks)


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_consume_run_windows_alarms_and_chunks(tmp_path: Path) -> None:
    """A complete run is consumed into windows, alarms and chunk markers."""
    run_dir = _make_run(tmp_path)
    database_path = tmp_path / "g15.db"

    summary = consume_run(run_dir, database_path, _consumer_config(tmp_path))

    assert summary.run_id
    assert summary.device_id == "DEV-C2"
    assert summary.shards_processed == 4
    # 512 samples, 64-sample windows, 4 shards of 128 samples -> 8 windows.
    assert summary.windows_written == 8
    assert summary.alarms_written == 0
    assert summary.windows_dropped == 0

    windows, alarms, chunks = _counts(database_path)
    assert windows == 8
    assert alarms == 0
    assert chunks == 4


def test_consumed_windows_carry_the_manifest_device_and_contiguous_positions(
    tmp_path: Path,
) -> None:
    """Every window is stored with the right device and a contiguous position."""
    run_dir = _make_run(tmp_path)
    database_path = tmp_path / "g15.db"
    consume_run(run_dir, database_path, _consumer_config(tmp_path))

    connection = sqlite3.connect(str(database_path))
    try:
        rows = connection.execute("""
            SELECT device_id, start_seq, end_seq_exclusive, features_json,
                   health_score
            FROM feature_windows ORDER BY start_seq
            """).fetchall()
    finally:
        connection.close()

    assert len(rows) == 8
    for index, (device_id, start_seq, end_seq, features_json, score) in enumerate(rows):
        assert device_id == "DEV-C2"
        assert start_seq == index * 64
        assert end_seq == start_seq + 64
        assert 0.0 <= score <= 100.0
        payload = json.loads(features_json)
        assert payload["features_version"] == "1"
        assert payload["sample_count"] == 64
        assert payload["window_start_seq"] == start_seq
        assert set(payload["features"]) == {
            "mean",
            "std",
            "rms",
            "ptp",
            "kurtosis_excess",
            "crest_factor",
            "dominant_freq_hz",
            "zero_crossing_rate",
        }


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


def test_consuming_the_same_run_twice_does_not_duplicate_rows(tmp_path: Path) -> None:
    """A repeat consumption writes nothing new."""
    run_dir = _make_run(tmp_path)
    database_path = tmp_path / "g15.db"
    config = _consumer_config(tmp_path)

    first = consume_run(run_dir, database_path, config)
    after_first = _counts(database_path)

    second = consume_run(run_dir, database_path, config)
    after_second = _counts(database_path)

    assert first.windows_written == 8
    assert second.windows_written == 0
    assert second.alarms_written == 0
    assert second.shards_processed == 0
    assert second.shards_skipped == 4
    assert after_first == after_second


def test_repeated_consumption_keeps_the_alarm_streak_alive(tmp_path: Path) -> None:
    """Skipping an already-processed shard must not reset the debounce state.

    This is the case where the streak would silently break: the shard is not
    analysed again, so the state has to come from the database rather than
    from a fresh start.
    """
    run_dir = _make_run(tmp_path)
    database_path = tmp_path / "g15.db"
    config = _consumer_config(tmp_path)

    consume_run(run_dir, database_path, config)

    connection = sqlite3.connect(str(database_path))
    try:
        row = connection.execute("SELECT state_json FROM alarm_states").fetchone()
    finally:
        connection.close()

    assert row is not None
    state = decode_state_json(str(row[0]))
    assert state.last_window_start_seq == 448
    assert state.scoring_version is not None
    assert state.rule_id is not None


# --------------------------------------------------------------------------
# transaction atomicity and replay
# --------------------------------------------------------------------------


def test_an_interrupted_consumption_leaves_no_partial_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while writing a shard rolls the whole shard back.

    The test injects a failure into the alarm-state write, which happens
    after the features of that shard have already been written inside the
    same transaction. If the transaction were not atomic, the features of
    the failed shard would survive while its chunk marker did not.
    """
    run_dir = _make_run(tmp_path)
    database_path = tmp_path / "g15.db"
    config = _consumer_config(tmp_path)

    def failing_upsert(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr(
        ConsumptionRepository, "upsert_alarm_state", failing_upsert, raising=True
    )

    with pytest.raises(Exception):
        consume_run(run_dir, database_path, config)

    # The failed shard's features must be gone, and its chunk marker too.
    connection = sqlite3.connect(str(database_path))
    try:
        windows = connection.execute("SELECT COUNT(*) FROM feature_windows").fetchone()[
            0
        ]
        chunks = connection.execute("SELECT COUNT(*) FROM processed_chunks").fetchone()[
            0
        ]
    finally:
        connection.close()

    assert windows == 0
    assert chunks == 0


def test_replaying_after_an_interruption_reaches_the_same_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replay after a failure produces the same rows as an uninterrupted run."""
    run_dir = _make_run(tmp_path)
    config = _consumer_config(tmp_path)

    broken_db = tmp_path / "broken.db"

    def failing_upsert(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr(
        ConsumptionRepository, "upsert_alarm_state", failing_upsert, raising=True
    )
    with pytest.raises(Exception):
        consume_run(run_dir, broken_db, config)

    # The replay into the same database must succeed and reach the full result.
    monkeypatch.undo()
    consume_run(run_dir, broken_db, config)

    clean_db = tmp_path / "clean.db"
    consume_run(run_dir, clean_db, config)

    assert _counts(broken_db) == _counts(clean_db)


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_a_failed_manifest_is_refused(tmp_path: Path) -> None:
    """Only a complete run may be consumed."""
    run_dir = _make_run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "failed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ConsumptionError, match="complete"):
        consume_run(run_dir, tmp_path / "g15.db", _consumer_config(tmp_path))


def test_a_changed_digest_under_the_same_part_id_is_refused(tmp_path: Path) -> None:
    """A shard whose content changed under the same part id is refused."""
    run_dir = _make_run(tmp_path)
    database_path = tmp_path / "g15.db"
    config = _consumer_config(tmp_path)
    consume_run(run_dir, database_path, config)

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["parts"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(Exception, match="sha256|contract|refusing to replay"):
        consume_run(run_dir, database_path, config)


def test_a_gap_between_shards_is_refused(tmp_path: Path) -> None:
    """A sample sequence with a hole is refused rather than analysed."""
    run_dir = _make_run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Rewrite the second shard so that its sequence starts later than the
    # first shard ended: a gap.
    shard = run_dir / manifest["parts"][1]["path"]
    with np.load(shard, allow_pickle=False) as arrays:
        seq = arrays["seq"].copy()
        timestamp_ns = arrays["timestamp_ns"].copy()
        vibration = arrays["vibration"].copy()
        temperature = arrays["temperature"].copy()
    shifted = seq + 1000
    np.savez(
        shard,
        seq=shifted,
        timestamp_ns=timestamp_ns,
        vibration=vibration,
        temperature=temperature,
    )

    # The digest no longer matches, so the shard is refused -- and so is the
    # gap. Either way the consumer must not consume this run.
    with pytest.raises(ConsumptionError):
        consume_run(run_dir, tmp_path / "g15.db", _consumer_config(tmp_path))


def test_a_missing_manifest_is_refused(tmp_path: Path) -> None:
    """A run directory without a manifest is refused."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConsumptionError, match="manifest"):
        consume_run(empty, tmp_path / "g15.db", _consumer_config(tmp_path))


# --------------------------------------------------------------------------
# the state envelope
# --------------------------------------------------------------------------


def test_state_json_round_trips() -> None:
    """A state survives the JSON envelope unchanged."""
    state = AlarmState(
        active_level="yellow",
        consecutive_breaches=2,
        normal_windows=1,
        alarm_windows=1,
        last_window_start_seq=4096,
        recovered_at_seq=None,
        scoring_version="heuristic-v1",
        rule_id="g15-b2-health-v1",
    )
    assert decode_state_json(encode_state_json(state)) == state


def test_state_json_refuses_an_unknown_version() -> None:
    """An unrecognised envelope version is refused, not guessed at."""
    payload = {
        "state_schema_version": "2",
        "state": {
            "active_level": None,
            "consecutive_breaches": 0,
            "normal_windows": 0,
            "alarm_windows": 0,
            "last_window_start_seq": None,
            "recovered_at_seq": None,
            "scoring_version": "heuristic-v1",
            "rule_id": "g15-b2-health-v1",
        },
        "reset_event": None,
    }
    with pytest.raises(StateCompatibilityError, match="state_schema_version"):
        decode_state_json(json.dumps(payload))


def test_state_json_refuses_a_legacy_envelope_without_a_version() -> None:
    """A legacy envelope with no version is refused, not read as current."""
    payload = {
        "state": {
            "active_level": "yellow",
            "consecutive_breaches": 2,
            "normal_windows": 0,
            "alarm_windows": 1,
            "last_window_start_seq": 2048,
            "recovered_at_seq": None,
            "scoring_version": "heuristic-v1",
            "rule_id": "g15-b2-health-v1",
        }
    }
    with pytest.raises(StateCompatibilityError, match="state_schema_version"):
        decode_state_json(json.dumps(payload))


def test_state_json_refuses_an_unknown_state_field() -> None:
    """An unexpected field in the state section is refused."""
    payload = {
        "state_schema_version": "1",
        "state": {
            "active_level": None,
            "consecutive_breaches": 0,
            "normal_windows": 0,
            "alarm_windows": 0,
            "last_window_start_seq": None,
            "recovered_at_seq": None,
            "scoring_version": "heuristic-v1",
            "rule_id": "g15-b2-health-v1",
            "surprise": 1,
        },
        "reset_event": None,
    }
    with pytest.raises(StateCompatibilityError, match="unknown state field"):
        decode_state_json(json.dumps(payload))


def test_state_json_refuses_a_float_counter() -> None:
    """A counter stored as a float is refused: it must be a JSON integer."""
    payload = {
        "state_schema_version": "1",
        "state": {
            "active_level": None,
            "consecutive_breaches": 2.0,
            "normal_windows": 0,
            "alarm_windows": 0,
            "last_window_start_seq": None,
            "recovered_at_seq": None,
            "scoring_version": "heuristic-v1",
            "rule_id": "g15-b2-health-v1",
        },
        "reset_event": None,
    }
    with pytest.raises(StateCompatibilityError, match="consecutive_breaches"):
        decode_state_json(json.dumps(payload))


def test_state_json_refuses_a_bool_counter() -> None:
    """A counter stored as a boolean is refused: bool is not a count."""
    payload = {
        "state_schema_version": "1",
        "state": {
            "active_level": None,
            "consecutive_breaches": True,
            "normal_windows": 0,
            "alarm_windows": 0,
            "last_window_start_seq": None,
            "recovered_at_seq": None,
            "scoring_version": "heuristic-v1",
            "rule_id": "g15-b2-health-v1",
        },
        "reset_event": None,
    }
    with pytest.raises(StateCompatibilityError, match="consecutive_breaches"):
        decode_state_json(json.dumps(payload))


def test_state_json_refuses_a_missing_state_field() -> None:
    """A state section missing a field is refused."""
    payload = {
        "state_schema_version": "1",
        "state": {
            "active_level": None,
            "consecutive_breaches": 0,
            "normal_windows": 0,
            "alarm_windows": 0,
            "last_window_start_seq": None,
            "recovered_at_seq": None,
            "scoring_version": "heuristic-v1",
        },
        "reset_event": None,
    }
    with pytest.raises(StateCompatibilityError, match="missing state field"):
        decode_state_json(json.dumps(payload))


# --------------------------------------------------------------------------
# the database itself
# --------------------------------------------------------------------------


def test_the_schema_has_the_five_section_nine_tables(tmp_path: Path) -> None:
    """Section 9's five tables are created."""
    connection = connect(tmp_path / "g15.db")
    try:
        initialize_database(connection)
        names = set(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        )
    finally:
        connection.close()

    assert {
        "runs",
        "processed_chunks",
        "feature_windows",
        "alarms",
        "alarm_states",
    } <= names


def test_foreign_keys_are_on(tmp_path: Path) -> None:
    """The foreign key pragma is on, so section 9's references are enforced."""
    connection = connect(tmp_path / "g15.db")
    try:
        value = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        connection.close()
    assert int(value) == 1


def test_wal_journalling_is_on(tmp_path: Path) -> None:
    """The WAL journal mode is on."""
    connection = connect(tmp_path / "g15.db")
    try:
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()
    assert mode == "wal"


def test_a_health_score_outside_the_range_is_refused(tmp_path: Path) -> None:
    """The CHECK constraint on the health score is enforced."""
    connection = connect(tmp_path / "g15.db")
    try:
        initialize_database(connection)
        connection.execute("""
            INSERT INTO runs (run_id, scenario_id, schema_version,
                              scoring_version, status, started_at)
            VALUES ('r', 's', '1.0', 'v', 'complete', '2026-10-14T00:00:00Z')
            """)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("""
                INSERT INTO feature_windows
                  (run_id, device_id, start_seq, end_seq_exclusive,
                   features_json, health_score)
                VALUES ('r', 'd', 0, 64, '{}', 101.0)
                """)
    finally:
        connection.close()


def test_an_explicit_reset_archives_the_previous_state(tmp_path: Path) -> None:
    """The explicit reset keeps the previous state for auditing."""
    connection = connect(tmp_path / "g15.db")
    try:
        initialize_database(connection)
        repository = ConsumptionRepository(connection)
        repository.ensure_run("r", "s", "1.0", "v", "complete", "2026-10-14T00:00:00Z")
        state = AlarmState(
            active_level="yellow",
            consecutive_breaches=2,
            alarm_windows=1,
            last_window_start_seq=2048,
            scoring_version="heuristic-v1",
            rule_id="g15-b2-health-v1",
        )
        repository.upsert_alarm_state(
            "r", "d", "g15-b2-health-v1", 2112, encode_state_json(state)
        )

        new_state = repository.reset_alarm_state(
            "r",
            "d",
            "g15-b2-health-v1",
            reason="rule_change",
            reset_at="2026-10-23T00:00:00Z",
        )

        assert new_state.consecutive_breaches == 0
        assert new_state.active_level is None

        row = connection.execute("SELECT state_json FROM alarm_states").fetchone()
        payload = json.loads(str(row[0]))
        assert payload["reset_event"]["reason"] == "rule_change"
        assert payload["reset_event"]["previous_state"]["consecutive_breaches"] == 2
    finally:
        connection.close()


def test_the_storage_package_is_importable() -> None:
    """The storage package exposes the names the consumer relies on."""
    from g15.storage import database, repository

    assert hasattr(database, "connect")
    assert hasattr(repository, "ConsumptionRepository")
