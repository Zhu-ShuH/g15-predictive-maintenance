"""Contract tests for the public ``reset-alarms`` entry (Issues #127/#143).

The original reset contract cases remain alongside the append-only audit,
hash-chain, migration and single-transaction cases added for #143. Every case
uses a real run directory produced by the project's producer and a real SQLite
database; the storage layer is never mocked.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from g15.analysis.alarms import AlarmState
from g15.config import load_config, load_consumer_config
from g15.pipeline.consumer import (
    AlarmResetResult,
    ConsumptionError,
    consume_run,
    load_manifest_from_run_dir,
    reset_alarm_state,
)
from g15.pipeline.producer import produce_run
from g15.storage.database import DatabaseError, connect
from g15.storage.repository import (
    AuditChainError,
    ConsumptionRepository,
    StateCompatibilityError,
    StorageError,
    decode_state_json,
    encode_state_json,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"
DATABASE_RELATIVE = Path("artifacts") / "results.sqlite"
RULE_ID = "health_heuristic"
OTHER_TABLES = ("processed_chunks", "feature_windows", "alarms", "runs")

#: A legacy envelope: parseable, but written before the version field existed.
LEGACY_TEXT = json.dumps(
    {
        "state": {
            "active_level": "yellow",
            "consecutive_breaches": 2,
            "normal_windows": 0,
            "alarm_windows": 1,
            "last_window_start_seq": 2048,
            "recovered_at_seq": None,
            "scoring_version": "heuristic-v0",
            "rule_id": RULE_ID,
        }
    }
)

#: An envelope from a future writer, carrying a field this version ignores.
FUTURE_TEXT = json.dumps(
    {
        "state_schema_version": "99",
        "state": {
            "active_level": "red",
            "consecutive_breaches": 4,
            "normal_windows": 0,
            "alarm_windows": 2,
            "last_window_start_seq": 4096,
            "recovered_at_seq": None,
            "scoring_version": "heuristic-v9",
            "rule_id": RULE_ID,
            "unrecognised_future_field": True,
        },
    }
)

#: A row whose JSON cannot be parsed at all.
DAMAGED_TEXT = "{ this is not JSON"


# --------------------------------------------------------------------------- #
# Fixtures and small helpers
# --------------------------------------------------------------------------- #


def _publish(tmp_path: Path) -> Path:
    """Publish one real run directory with the shipped healthy scenario."""
    return produce_run(load_config(CONFIGS / "healthy.yaml"), tmp_path / "runs")


def _manifest_text(run_dir: Path) -> str:
    return (run_dir / "manifest.json").read_text(encoding="utf-8")


def _device(run_dir: Path) -> str:
    return str(json.loads(_manifest_text(run_dir))["device_id"])


def _database(run_dir: Path) -> Path:
    return run_dir / DATABASE_RELATIVE


def _consume(run_dir: Path) -> None:
    """Consume every shard the manifest currently declares."""
    consume_run(
        run_dir,
        _database(run_dir),
        load_consumer_config(CONFIGS / "consumer.yaml"),
    )


def _consume_first_part_only(run_dir: Path) -> str:
    """Consume part 0, returning the untouched manifest for phase two."""
    original = _manifest_text(run_dir)
    document = json.loads(original)
    document["parts"] = document["parts"][:1]
    document["total_points"] = document["parts"][0]["rows"]
    (run_dir / "manifest.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    _consume(run_dir)
    return original


def _consume_remaining_parts(run_dir: Path, original: str) -> None:
    """Restore the full manifest and consume the shards left after a reset."""
    (run_dir / "manifest.json").write_text(original, encoding="utf-8")
    _consume(run_dir)


def _reset(
    run_dir: Path, *, reason: str = "operator reset", **kwargs: Any
) -> AlarmResetResult:
    """Call the frozen public entry with the run's real identifiers."""
    return reset_alarm_state(
        run_dir,
        _database(run_dir),
        _device(run_dir),
        RULE_ID,
        reason=reason,
        **kwargs,
    )


def _raw_text(database_path: Path) -> str:
    connection = sqlite3.connect(str(database_path))
    try:
        found = connection.execute("SELECT state_json FROM alarm_states").fetchone()
    finally:
        connection.close()
    assert found is not None
    return str(found[0])


def _row(database_path: Path) -> tuple[dict[str, Any], int]:
    connection = sqlite3.connect(str(database_path))
    try:
        found = connection.execute(
            "SELECT state_json, last_end_seq_exclusive FROM alarm_states"
        ).fetchone()
    finally:
        connection.close()
    assert found is not None
    return json.loads(str(found[0])), int(found[1])


def _set_raw_text(database_path: Path, text: str) -> None:
    connection = sqlite3.connect(str(database_path))
    try:
        connection.execute("UPDATE alarm_states SET state_json = ?", (text,))
        connection.commit()
    finally:
        connection.close()


def _audit_rows(database_path: Path) -> list[tuple[Any, ...]]:
    """Return the stable public columns of the append-only audit chain."""
    connection = sqlite3.connect(str(database_path))
    try:
        return connection.execute("""
            SELECT event_sequence, event_time_utc, action, run_id, device_id,
                   rule_id, actor_ref, reason, payload_json, previous_hash,
                   event_hash
            FROM audit_events
            ORDER BY event_sequence
            """).fetchall()
    finally:
        connection.close()


def _snapshot(database_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Full contents of the four tables a reset must never touch."""
    connection = sqlite3.connect(str(database_path))
    try:
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in OTHER_TABLES
        }
    finally:
        connection.close()


def _add_alarm(database_path: Path) -> None:
    """Seed a real alarm so 'untouched' is not merely a 0 -> 0 assertion."""
    connection = sqlite3.connect(str(database_path))
    try:
        run_id, device_id = connection.execute(
            "SELECT run_id, device_id FROM alarm_states"
        ).fetchone()
        connection.execute(
            "INSERT INTO alarms (run_id, device_id, rule_id, start_seq, level, "
            "reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                device_id,
                RULE_ID,
                4096,
                "yellow",
                "seeded for the reset contract",
                "2026-11-10T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# C2 proposal section 7, cases 1-5
# --------------------------------------------------------------------------- #


def test_reset_returns_a_complete_audit_and_archives_the_old_state(
    tmp_path: Path,
) -> None:
    """Case 1: a v1 row returns a full result and keeps the sequence anchor."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    original_text = _raw_text(_database(run_dir))
    _, original_anchor = _row(_database(run_dir))

    result = _reset(run_dir, reason="换规则后重算 DEV-002")

    assert result.run_id == load_manifest_from_run_dir(run_dir).run_id
    assert result.device_id == _device(run_dir)
    assert result.rule_id == RULE_ID
    assert result.reason == "换规则后重算 DEV-002"
    assert result.reset_at_utc.endswith("Z")
    assert result.previous_state_schema_version == "1"
    assert result.previous_state is not None
    assert result.previous_state_decodable is True
    assert result.previous_state_json == original_text
    assert result.archived_reset_count == 0
    assert result.new_state.consecutive_breaches == 0
    assert result.new_state.active_level is None
    assert result.audit_sequence == 1
    assert result.audit_previous_hash is None
    assert len(result.audit_event_hash) == 64

    payload, anchor = _row(_database(run_dir))
    assert payload["reset_event"]["reason"] == result.reason
    assert payload["reset_event"]["reset_at_utc"] == result.reset_at_utc
    assert payload["reset_event"]["previous_state_json"] == original_text
    assert payload["reset_event"]["previous_state_schema_version"] == "1"
    assert anchor == original_anchor  # the anchor is deliberately kept
    assert decode_state_json(_raw_text(_database(run_dir))).consecutive_breaches == 0

    rows = _audit_rows(_database(run_dir))
    assert len(rows) == 1
    assert rows[0][0] == result.audit_sequence
    assert rows[0][2] == "alarm_state.reset"
    assert rows[0][3:6] == (result.run_id, result.device_id, result.rule_id)
    assert rows[0][6] == "local-cli"
    assert rows[0][7] == result.reason
    assert rows[0][9] is None
    assert rows[0][10] == result.audit_event_hash
    audit_payload = json.loads(str(rows[0][8]))
    assert audit_payload["previous_state_decodable"] is True
    assert audit_payload["previous_state_schema_version"] == "1"
    assert len(audit_payload["previous_state_sha256"]) == 64

    connection = connect(_database(run_dir))
    try:
        assert ConsumptionRepository(connection).verify_audit_chain() == 1
    finally:
        connection.close()


def test_reset_then_continue_consumption_starts_a_fresh_streak(
    tmp_path: Path,
) -> None:
    """Case 2: unprocessed shards still run after a reset, processed ones skip."""
    run_dir = _publish(tmp_path)
    original = _consume_first_part_only(run_dir)
    windows_before = len(_snapshot(_database(run_dir))["feature_windows"])

    result = _reset(run_dir)
    _consume_remaining_parts(run_dir, original)

    after = _snapshot(_database(run_dir))
    assert len(after["feature_windows"]) > windows_before
    assert len(after["processed_chunks"]) == 2
    payload, _ = _row(_database(run_dir))
    assert payload["reset_event"]["reason"] == result.reason


def test_later_window_writes_keep_the_reset_event(tmp_path: Path) -> None:
    """Case 3: routine window writes re-attach the audit, repeatedly."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    result = _reset(run_dir)

    connection = connect(_database(run_dir))
    try:
        repository = ConsumptionRepository(connection)
        for index in range(1, 4):
            fresh = AlarmState(
                consecutive_breaches=index,
                scoring_version="heuristic-v1",
                rule_id=RULE_ID,
            )
            repository.upsert_alarm_state(
                result.run_id,
                result.device_id,
                RULE_ID,
                199936 + 64 * index,
                encode_state_json(fresh),
            )
    finally:
        connection.close()

    payload, _ = _row(_database(run_dir))
    assert payload["reset_event"]["reason"] == result.reason
    assert decode_state_json(_raw_text(_database(run_dir))).consecutive_breaches == 3


def test_audit_survives_reopening_the_database(tmp_path: Path) -> None:
    """Case 4: closing and re-opening the database does not lose the audit."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    result = _reset(run_dir)

    connection = connect(_database(run_dir))
    try:
        found = connection.execute("SELECT state_json FROM alarm_states").fetchone()
    finally:
        connection.close()

    assert found is not None
    payload = json.loads(str(found[0]))
    assert payload["reset_event"]["reason"] == result.reason
    assert decode_state_json(str(found[0])).consecutive_breaches == 0
    rows = _audit_rows(_database(run_dir))
    assert len(rows) == 1
    assert rows[0][10] == result.audit_event_hash


def test_second_reset_archives_the_first_in_history(tmp_path: Path) -> None:
    """Case 5: oldest-first history, and the count excludes the current reset."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    first = _reset(run_dir, reason="first")
    second = _reset(run_dir, reason="second")

    assert first.archived_reset_count == 0
    assert second.archived_reset_count == 1
    assert first.audit_sequence == 1
    assert second.audit_sequence == 2
    assert second.audit_previous_hash == first.audit_event_hash
    payload, _ = _row(_database(run_dir))
    assert payload["reset_event"]["reason"] == "second"
    assert [item["reason"] for item in payload["reset_history"]] == ["first"]

    rows = _audit_rows(_database(run_dir))
    assert [row[0] for row in rows] == [1, 2]
    assert rows[1][9] == rows[0][10]
    connection = connect(_database(run_dir))
    try:
        assert ConsumptionRepository(connection).verify_audit_chain() == 2
    finally:
        connection.close()


def test_reset_migrates_an_existing_database_without_audit_schema(
    tmp_path: Path,
) -> None:
    """The reset write entry upgrades a pre-#143 database before appending."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    database = _database(run_dir)

    connection = sqlite3.connect(str(database))
    try:
        connection.execute("DROP TABLE audit_events")
        connection.commit()
    finally:
        connection.close()

    result = _reset(run_dir, reason="migrated write entry")

    assert result.audit_sequence == 1
    assert result.audit_previous_hash is None
    assert len(_audit_rows(database)) == 1
    connection = sqlite3.connect(str(database))
    try:
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert {
        "audit_events_append_guard",
        "audit_events_no_update",
        "audit_events_no_delete",
    }.issubset(triggers)


@pytest.mark.parametrize(
    "statement",
    (
        "UPDATE audit_events SET reason = 'rewritten' WHERE event_sequence = 1",
        "DELETE FROM audit_events WHERE event_sequence = 1",
        "INSERT OR REPLACE INTO audit_events "
        "SELECT * FROM audit_events WHERE event_sequence = 1",
    ),
)
def test_audit_rows_cannot_be_rewritten(tmp_path: Path, statement: str) -> None:
    """Triggers reject UPDATE, DELETE and SQLite REPLACE rewrite paths."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    result = _reset(run_dir, reason="immutable")

    connection = sqlite3.connect(str(_database(run_dir)))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)
        connection.rollback()
    finally:
        connection.close()

    rows = _audit_rows(_database(run_dir))
    assert len(rows) == 1
    assert rows[0][7] == "immutable"
    assert rows[0][10] == result.audit_event_hash


@pytest.mark.parametrize(
    "statement",
    (
        "INSERT INTO audit_events "
        "SELECT event_sequence + 2, event_time_utc, action, run_id, device_id, "
        "rule_id, actor_ref, reason, payload_json, event_hash, "
        "lower(hex(randomblob(32))) "
        "FROM audit_events WHERE event_sequence = 1",
        "INSERT INTO audit_events "
        "SELECT event_sequence + 1, event_time_utc, action, run_id, device_id, "
        "rule_id, actor_ref, reason, payload_json, lower(hex(zeroblob(32))), "
        "lower(hex(randomblob(32))) "
        "FROM audit_events WHERE event_sequence = 1",
        "INSERT OR REPLACE INTO audit_events "
        "SELECT event_sequence + 1, event_time_utc, action, run_id, device_id, "
        "rule_id, actor_ref, reason, payload_json, event_hash, event_hash "
        "FROM audit_events WHERE event_sequence = 1",
    ),
)
def test_audit_insert_must_extend_the_exact_chain_tail(
    tmp_path: Path, statement: str
) -> None:
    """Direct writers cannot skip a sequence or name the wrong predecessor."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    _reset(run_dir, reason="tail anchor")
    database = _database(run_dir)
    before = _audit_rows(database)

    connection = sqlite3.connect(str(database))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)
        connection.rollback()
    finally:
        connection.close()

    assert _audit_rows(database) == before


def test_audit_chain_detects_an_offline_payload_rewrite(tmp_path: Path) -> None:
    """A privileged trigger bypass remains detectable by the hash-chain check."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    _reset(run_dir, reason="first")
    _reset(run_dir, reason="second")
    database = _database(run_dir)

    # This deliberately models a file/DDL administrator, which is outside the
    # trigger threat boundary.  Dropping the trigger makes the tamper possible;
    # verification must still refuse the now-inconsistent chain.
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute(
            "UPDATE audit_events "
            "SET payload_json = '{\"tampered\":true}' "
            "WHERE event_sequence = 1"
        )
        connection.commit()
    finally:
        connection.close()

    connection = connect(database)
    try:
        with pytest.raises(AuditChainError, match="audit event 1 hash mismatch"):
            ConsumptionRepository(connection).verify_audit_chain()
    finally:
        connection.close()


def test_audit_chain_detects_a_broken_previous_hash(tmp_path: Path) -> None:
    """The verifier rejects a link that no longer names its predecessor."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    _reset(run_dir, reason="first")
    _reset(run_dir, reason="second")
    database = _database(run_dir)

    connection = sqlite3.connect(str(database))
    try:
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute(
            "UPDATE audit_events SET previous_hash = ? WHERE event_sequence = 2",
            ("0" * 64,),
        )
        connection.commit()
    finally:
        connection.close()

    connection = connect(database)
    try:
        with pytest.raises(AuditChainError, match="audit event 2 has previous_hash"):
            ConsumptionRepository(connection).verify_audit_chain()
    finally:
        connection.close()


def test_audit_insert_failure_rolls_back_the_state_reset(tmp_path: Path) -> None:
    """State and audit are atomic: refusing the INSERT preserves both sides."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    database = _database(run_dir)
    original_text = _raw_text(database)

    connection = sqlite3.connect(str(database))
    try:
        connection.execute("""
            CREATE TRIGGER reject_audit_insert
            BEFORE INSERT ON audit_events
            BEGIN
              SELECT RAISE(ABORT, 'forced audit failure');
            END
            """)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(sqlite3.IntegrityError, match="forced audit failure"):
        _reset(run_dir, reason="must roll back")

    assert _raw_text(database) == original_text
    assert _audit_rows(database) == []


def test_normal_state_writes_do_not_remove_append_only_audit(tmp_path: Path) -> None:
    """Later window-state writes leave the independent audit chain untouched."""
    run_dir = _publish(tmp_path)
    original = _consume_first_part_only(run_dir)
    result = _reset(run_dir, reason="before remaining shards")
    before = _audit_rows(_database(run_dir))

    _consume_remaining_parts(run_dir, original)

    after = _audit_rows(_database(run_dir))
    assert after == before
    assert after[0][10] == result.audit_event_hash


# --------------------------------------------------------------------------- #
# C2 proposal section 7, cases 6-9: legacy, future and damaged rows
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "label,text,expected_version",
    [
        ("legacy", LEGACY_TEXT, None),
        ("future", FUTURE_TEXT, "99"),
        ("damaged", DAMAGED_TEXT, None),
    ],
)
def test_undecodable_rows_are_archived_verbatim(
    tmp_path: Path, label: str, text: str, expected_version: str | None
) -> None:
    """Cases 6-8: the escape hatch works; the original text is never lost."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    _set_raw_text(_database(run_dir), text)

    result = _reset(run_dir, reason=label)

    assert result.previous_state_decodable is False
    assert result.previous_state is None
    assert result.previous_state_json == text
    assert result.previous_state_schema_version == expected_version
    assert result.new_state.consecutive_breaches == 0
    payload, _ = _row(_database(run_dir))
    assert payload["state_schema_version"] == "1"
    assert payload["reset_event"]["previous_state_json"] == text
    assert payload["reset_event"]["previous_state_schema_version"] == (
        None if expected_version is None else expected_version
    )
    if label == "damaged":
        assert "previous_state" not in payload["reset_event"]
    else:
        assert payload["reset_event"]["previous_state"]["consecutive_breaches"] >= 2


@pytest.mark.parametrize("text", [LEGACY_TEXT, FUTURE_TEXT, DAMAGED_TEXT])
def test_undecodable_rows_are_refused_when_legacy_is_disabled(
    tmp_path: Path, text: str
) -> None:
    """Case 9: strict mode refuses *before* writing, leaving the row intact."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    _set_raw_text(_database(run_dir), text)

    with pytest.raises(StateCompatibilityError):
        _reset(run_dir, allow_legacy=False)

    assert _raw_text(_database(run_dir)) == text


# --------------------------------------------------------------------------- #
# C2 proposal section 7, cases 10-16
# --------------------------------------------------------------------------- #


def test_missing_state_is_a_storage_error(tmp_path: Path) -> None:
    """Case 10: nothing to reset is a storage failure, not a silent no-op."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    connection = sqlite3.connect(str(_database(run_dir)))
    try:
        connection.execute("DELETE FROM alarm_states")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageError, match="nothing to reset"):
        _reset(run_dir)


@pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
def test_empty_reason_is_a_value_error(tmp_path: Path, reason: str) -> None:
    """Case 11: empty or whitespace-only reasons are argument errors (2)."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    with pytest.raises(ValueError, match="non-empty"):
        _reset(run_dir, reason=reason)
    assert '"reset_event": null' in _raw_text(_database(run_dir))


def test_device_mismatch_is_a_consumption_error(tmp_path: Path) -> None:
    """Case 12: the device must agree with the manifest (A's ruling 4)."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    original = _raw_text(_database(run_dir))

    with pytest.raises(ConsumptionError, match="does not match"):
        reset_alarm_state(
            run_dir,
            _database(run_dir),
            "ANOTHER-DEVICE",
            RULE_ID,
            reason="wrong device",
        )
    assert _raw_text(_database(run_dir)) == original


@pytest.mark.parametrize("mode", ["missing", "malformed"])
def test_bad_manifest_is_a_consumption_error(tmp_path: Path, mode: str) -> None:
    """Case 13: a missing or damaged manifest is a consumption failure (3)."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    device = _device(run_dir)
    original = _raw_text(_database(run_dir))
    path = run_dir / "manifest.json"
    if mode == "missing":
        path.unlink()
    else:
        path.write_text("{ this is not JSON", encoding="utf-8")

    with pytest.raises(ConsumptionError):
        reset_alarm_state(
            run_dir, _database(run_dir), device, RULE_ID, reason="bad manifest"
        )
    assert _raw_text(_database(run_dir)) == original


def test_missing_database_is_a_database_error_and_is_not_created(
    tmp_path: Path,
) -> None:
    """Case 14: a reset never creates an empty database."""
    run_dir = _publish(tmp_path)
    database = _database(run_dir)
    assert not database.exists()

    with pytest.raises(DatabaseError, match="no database"):
        reset_alarm_state(
            run_dir, database, _device(run_dir), RULE_ID, reason="no database"
        )

    assert not database.exists()


def test_reset_changes_no_other_table(tmp_path: Path) -> None:
    """Case 15: the four other tables keep every row, byte for byte."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    _add_alarm(_database(run_dir))
    before = _snapshot(_database(run_dir))
    assert before["alarms"]  # a real row, not a vacuous 0 -> 0 check
    assert before["feature_windows"]
    assert before["processed_chunks"]

    _reset(run_dir)

    assert _snapshot(_database(run_dir)) == before


def test_a_failed_consume_never_resets_the_state(tmp_path: Path) -> None:
    """Case 16: consumption stops on a bad state and never self-heals it."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    _set_raw_text(_database(run_dir), DAMAGED_TEXT)
    before = _raw_text(_database(run_dir))

    # A damaged envelope is refused by the strict reader as a StorageError;
    # the important part is that consumption stops and never repairs the row.
    with pytest.raises(StorageError):
        _consume(run_dir)

    assert _raw_text(_database(run_dir)) == before
    assert "reset_event" not in before


# --------------------------------------------------------------------------- #
# A's ruling 8: the two extra acceptance cases
# --------------------------------------------------------------------------- #


def test_replaying_committed_shards_after_a_reset_is_skipped(
    tmp_path: Path,
) -> None:
    """Extra 1: replaying committed shards does not duplicate or re-alarm."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    _add_alarm(_database(run_dir))
    result = _reset(run_dir)
    before = _snapshot(_database(run_dir))

    _consume(run_dir)

    assert _snapshot(_database(run_dir)) == before
    payload, _ = _row(_database(run_dir))
    assert payload["reset_event"]["reason"] == result.reason
    assert len(payload.get("reset_history", [])) == result.archived_reset_count


def test_audit_survives_new_shards_and_a_reopen(tmp_path: Path) -> None:
    """Extra 2: reset -> new shard consumed -> reopen -> audit still readable."""
    run_dir = _publish(tmp_path)
    original = _consume_first_part_only(run_dir)
    result = _reset(run_dir)
    _consume_remaining_parts(run_dir, original)

    connection = connect(_database(run_dir))
    try:
        found = connection.execute("SELECT state_json FROM alarm_states").fetchone()
    finally:
        connection.close()

    assert found is not None
    payload = json.loads(str(found[0]))
    assert payload["reset_event"]["reason"] == result.reason
    assert payload["reset_event"]["previous_state_json"] == result.previous_state_json
    rows = _audit_rows(_database(run_dir))
    assert len(rows) == 1
    assert rows[0][10] == result.audit_event_hash


# --------------------------------------------------------------------------- #
# A's ruling 7 / C4-7: one transaction, and the public names
# --------------------------------------------------------------------------- #


def test_read_and_write_share_one_transaction(tmp_path: Path) -> None:
    """The SELECT and the UPDATE sit between the same BEGIN and COMMIT."""
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    connection = connect(_database(run_dir))
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        ConsumptionRepository(connection).reset_alarm_state(
            load_manifest_from_run_dir(run_dir).run_id,
            _device(run_dir),
            RULE_ID,
            reason="atomic",
        )
    finally:
        connection.set_trace_callback(None)
        connection.close()

    begin = next(
        i
        for i, sql in enumerate(statements)
        if sql.strip().startswith("BEGIN IMMEDIATE")
    )
    select = next(i for i, sql in enumerate(statements) if "SELECT state_json" in sql)
    update = next(
        i
        for i, sql in enumerate(statements)
        if sql.strip().startswith("UPDATE alarm_states")
    )
    audit_insert = next(
        i
        for i, sql in enumerate(statements)
        if sql.strip().startswith("INSERT INTO audit_events")
    )
    commit = next(
        i for i, sql in enumerate(statements) if sql.strip().startswith("COMMIT")
    )
    assert begin < select < update < audit_insert < commit


def test_a_concurrent_writer_cannot_interleave_with_the_reset(
    tmp_path: Path,
) -> None:
    """A rival writer is refused between the reset's read and its write.

    ``transaction()`` opens with ``BEGIN IMMEDIATE``, so the write lock is
    already held when the reset reads the old row. This probe injects a second
    connection exactly at that point (after ``SELECT``, before ``UPDATE``) and
    asserts SQLite refuses it with a lock error rather than letting it replace
    the row that is about to be archived. This is a deterministic form of the
    concurrent-writer probe C4 announced for the implementation PR.
    """
    run_dir = _publish(tmp_path)
    _consume(run_dir)
    database = _database(run_dir)
    connection = connect(database)
    refusals: list[str] = []

    def rival_probe(sql: str) -> None:
        if "SELECT state_json" not in sql or refusals:
            return
        rival = sqlite3.connect(str(database), timeout=0)
        try:
            try:
                rival.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                refusals.append(str(exc))
            else:
                rival.execute("ROLLBACK")
        finally:
            rival.close()

    connection.set_trace_callback(rival_probe)
    try:
        audit = ConsumptionRepository(connection).reset_alarm_state_audited(
            load_manifest_from_run_dir(run_dir).run_id,
            _device(run_dir),
            RULE_ID,
            reason="concurrent writer probe",
        )
    finally:
        connection.set_trace_callback(None)
        connection.close()

    assert len(refusals) == 1
    assert "lock" in refusals[0].lower()
    assert audit.previous_state_json


def test_the_public_names_are_exported() -> None:
    """Section 8.5 makes both names part of the consumer's public surface."""
    from g15.pipeline import consumer

    assert "reset_alarm_state" in consumer.__all__
    assert "AlarmResetResult" in consumer.__all__
    assert callable(consumer.reset_alarm_state)
