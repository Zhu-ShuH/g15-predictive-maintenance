"""Fault-injection tests for the independent validator (Issue #9).

``docs/acceptance.md`` section 4 asks for at least three *independent* pieces of
fault-injection evidence: a business fault, a corrupted file/SHA, and recovery
from ``kill -9`` on Linux.  The controlled waveforms already cover the business
fault in ``tests/integration/test_validation_report.py``; this module covers
the two reliability classes and the validator's own reaction to them.

Everything here corrupts a real run produced by the real producer: no shard is
hand-built, because the point is to show what the pipeline does with damaged
input, not what a fixture can be made to look like.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from g15.config import load_config, load_consumer_config
from g15.pipeline.consumer import ConsumptionError, consume_run
from g15.pipeline.producer import produce_run_detailed
from g15.pipeline.serialization import ChecksumMismatchError
from g15.reporting.builder import build_report
from g15.storage.database import connect, initialize_database

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPOSITORY_ROOT / "configs"


def make_run(tmp_path: Path, profile: str = "acceptance_spike_seed42") -> Path:
    """Produce a complete run for the fault-injection tests.

    Args:
        tmp_path: Directory that receives the run.
        profile: Configuration file stem under ``configs/``.

    Returns:
        The published run directory.
    """
    config = load_config(CONFIGS / f"{profile}.yaml")
    return produce_run_detailed(config, tmp_path).run_dir


def first_shard(run_dir: Path) -> Path:
    """Return the path of the run's first shard, as the manifest names it."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return run_dir / manifest["parts"][0]["path"]


def consumed_rows(database: Path) -> int:
    """Return the number of feature windows stored in ``database``."""
    connection = connect(database)
    try:
        row = connection.execute("SELECT COUNT(*) FROM feature_windows").fetchone()
    finally:
        connection.close()
    return 0 if row is None else int(row[0])


# -- class 2: corrupted file / digest mismatch ------------------------------


def test_corrupted_shard_bytes_are_refused_by_the_digest(tmp_path: Path) -> None:
    """Flipping one byte of a shard must abort the whole consumption.

    The manifest digest is the only guard between a damaged file and the
    database, so this asserts both the exception type and that nothing was
    written before the refusal.
    """
    run_dir = make_run(tmp_path)
    database = run_dir / "artifacts" / "results.sqlite"
    shard = first_shard(run_dir)
    payload = bytearray(shard.read_bytes())
    payload[len(payload) // 2] ^= 0xFF
    shard.write_bytes(bytes(payload))

    # The consumer re-raises the serialization failure as its own error type and
    # keeps the digest message, so both the type and the reason are asserted.
    with pytest.raises(ConsumptionError, match="sha256 is"):
        consume_run(run_dir, database, load_consumer_config(CONFIGS / "consumer.yaml"))

    assert not database.exists() or consumed_rows(database) == 0


def test_missing_shard_is_refused(tmp_path: Path) -> None:
    """A deleted shard must fail the run rather than silently skip samples."""
    run_dir = make_run(tmp_path)
    database = run_dir / "artifacts" / "results.sqlite"
    first_shard(run_dir).unlink()

    with pytest.raises((ConsumptionError, OSError)):
        consume_run(run_dir, database, load_consumer_config(CONFIGS / "consumer.yaml"))

    assert not database.exists() or consumed_rows(database) == 0


def test_truncated_shard_is_refused(tmp_path: Path) -> None:
    """A truncated NPZ must be rejected as a data contract failure."""
    run_dir = make_run(tmp_path)
    database = run_dir / "artifacts" / "results.sqlite"
    shard = first_shard(run_dir)
    data = shard.read_bytes()
    shard.write_bytes(data[: len(data) // 2])

    with pytest.raises((ChecksumMismatchError, ConsumptionError, OSError, ValueError)):
        consume_run(run_dir, database, load_consumer_config(CONFIGS / "consumer.yaml"))

    assert not database.exists() or consumed_rows(database) == 0


def test_validator_never_reads_the_truth_file_through_the_consumer(
    tmp_path: Path,
) -> None:
    """Deleting ``private_truth`` must not change what the consumer stores.

    ``docs/interface.md`` section 5.3 keeps truth readable only by the
    validator; if the consumer needed it, this test would fail.  The report then
    degrades to "no injected event" instead of inventing a detection rate.
    """
    run_dir = make_run(tmp_path)
    database = run_dir / "artifacts" / "results.sqlite"
    shutil.rmtree(run_dir / "private_truth")

    consume_run(run_dir, database, load_consumer_config(CONFIGS / "consumer.yaml"))
    payload = json.loads(build_report(run_dir, database).read_text(encoding="utf-8"))

    assert payload["alarm_count"] == 1
    assert payload["fault_events"] == 0
    assert payload["detection_rate"] is None


def test_damaged_truth_file_fails_the_report_not_the_consumer(
    tmp_path: Path,
) -> None:
    """A broken truth file must stop the report, after consumption succeeded."""
    run_dir = make_run(tmp_path)
    database = run_dir / "artifacts" / "results.sqlite"
    consume_run(run_dir, database, load_consumer_config(CONFIGS / "consumer.yaml"))

    truth = run_dir / "private_truth" / "faults.json"
    truth.write_text(
        '{"run_id": "x", "events": [{"event_id": "F001"}]}', encoding="utf-8"
    )

    from g15.reporting.builder import ReportError  # noqa: PLC0415

    with pytest.raises(ReportError, match="invalid truth file"):
        build_report(run_dir, database)


# -- class 3: kill -9 recovery ---------------------------------------------


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "kill -9 是 POSIX 信号，Windows 没有等价语义；这一类证据按 "
        "docs/acceptance.md §4 必须在 Linux 上留证，Windows 上不伪造。"
    ),
)
def test_kill_dash_nine_replay_matches_an_uninterrupted_run(tmp_path: Path) -> None:
    """A consumer killed with ``kill -9`` must replay to the same rows.

    The consumer writes each shard in one transaction, so an interrupted pass
    may leave some shards processed and the rest not.  Re-running it must add
    exactly the missing shards and end with the same window and alarm counts as
    an uninterrupted run -- no duplicate rows, no lost shards.
    """
    run_dir = make_run(tmp_path)
    reference_dir = run_dir.parent / "reference"
    shutil.copytree(run_dir, reference_dir)

    reference_db = reference_dir / "artifacts" / "results.sqlite"
    consume_run(
        reference_dir, reference_db, load_consumer_config(CONFIGS / "consumer.yaml")
    )
    reference_rows = consumed_rows(reference_db)

    database = run_dir / "artifacts" / "results.sqlite"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from g15.config import load_consumer_config\n"
        "from g15.pipeline.consumer import consume_run\n"
        "consume_run(Path(sys.argv[1]), Path(sys.argv[2]), "
        "load_consumer_config(Path(sys.argv[3])))\n"
    )
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        [
            sys.executable,
            "-c",
            script,
            str(run_dir),
            str(database),
            str(CONFIGS / "consumer.yaml"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.35)
    process.send_signal(signal.SIGKILL)
    process.wait(timeout=30)

    consume_run(run_dir, database, load_consumer_config(CONFIGS / "consumer.yaml"))
    assert consumed_rows(database) == reference_rows


def test_partial_database_is_reusable_after_a_failed_shard(tmp_path: Path) -> None:
    """A refusal must leave the database usable, not wedged.

    The first shard is corrupted so the first pass dies; repairing the file and
    re-running must complete the run, which is the property the kill -9 case
    depends on and which is testable on every platform.
    """
    run_dir = make_run(tmp_path)
    database = run_dir / "artifacts" / "results.sqlite"
    shard = first_shard(run_dir)
    original = shard.read_bytes()
    payload = bytearray(original)
    payload[0] ^= 0xFF
    shard.write_bytes(bytes(payload))

    with pytest.raises(ConsumptionError, match="sha256 is"):
        consume_run(run_dir, database, load_consumer_config(CONFIGS / "consumer.yaml"))

    shard.write_bytes(original)
    summary = consume_run(
        run_dir, database, load_consumer_config(CONFIGS / "consumer.yaml")
    )
    assert summary.windows_written > 0
    assert consumed_rows(database) == summary.windows_written


def test_second_consumption_is_idempotent(tmp_path: Path) -> None:
    """Re-consuming an untouched run must add no window and no alarm."""
    run_dir = make_run(tmp_path)
    database = run_dir / "artifacts" / "results.sqlite"
    config = load_consumer_config(CONFIGS / "consumer.yaml")

    first = consume_run(run_dir, database, config)
    rows_after_first = consumed_rows(database)
    second = consume_run(run_dir, database, config)

    assert second.windows_written == 0
    assert second.alarms_written == 0
    assert second.shards_skipped == first.shards_processed
    assert consumed_rows(database) == rows_after_first


def test_report_survives_a_database_that_was_only_initialised(tmp_path: Path) -> None:
    """An empty but valid database must report honest zeros, not crash.

    The schema exists and the run really did inject one event, so nothing was
    detected: the rate must be ``0.0`` (not ``null``, which would claim the run
    had no events, and not ``100%``, which would be a lie).  Health bounds stay
    ``null`` because no window was scored.
    """
    run_dir = make_run(tmp_path)
    database = run_dir / "artifacts" / "results.sqlite"
    connection = connect(database)
    try:
        initialize_database(connection)
    finally:
        connection.close()

    payload = json.loads(build_report(run_dir, database).read_text(encoding="utf-8"))
    assert payload["feature_windows"] == 0
    assert payload["alarm_count"] == 0
    assert payload["health_score_min"] is None
    assert payload["health_score_max"] is None
    assert payload["fault_events"] == 1
    assert payload["detected_events"] == 0
    assert payload["detection_rate"] == 0.0
    assert payload["passed"] is False
    assert payload["processed_points"] == 0
    assert payload["rejected_points"] == payload["total_points"]
