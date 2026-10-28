"""Tests for the final-shard row bound (Issue #69, owner C2).

``docs/interface.md`` section 8.1 derives the final shard's length from the
nominal shard size: a run of ``n`` shards has ``n-1`` shards of
``chunk_points`` rows and a final one of ``total_points - (n-1) *
chunk_points`` rows, which is **at most** ``chunk_points``. The bound is
inclusive -- when ``total_points`` is an exact multiple, the final shard
holds exactly ``chunk_points`` rows.

The consumer can read the nominal shard size back from the shards that are
not the tail, so it can check the tail against it. Before Issue #69 it did
not, and a run whose shards were cut ``[128, 256]`` was accepted even though
its final shard was twice the nominal length.

Every test here builds a **real** run directory -- real NPZ shards written
through C4's ``write_chunk_atomic`` and a real ``manifest.json`` -- and
consumes it through the public ``consume_run`` entry point. Nothing calls the
private helpers directly, so the tests exercise the same path an operator
would.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from g15.common.types import SimulationChunk
from g15.config import ConsumerConfig, load_consumer_config
from g15.pipeline.consumer import ConsumptionError, consume_run
from g15.pipeline.schema import Manifest, ManifestStatus, manifest_to_dict
from g15.pipeline.serialization import write_chunk_atomic

REPO_ROOT = Path(__file__).resolve().parents[2]

SAMPLE_RATE_HZ = 100
START_TIME_UTC = "2026-10-14T00:00:00Z"
DEVICE_ID = "DEV-C2"
SCENARIO_ID = "c2_last_shard_bound"


def _consumer_config(tmp_path: Path, **overrides: object) -> ConsumerConfig:
    """Build a consumer configuration with a small window."""
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


def _build_run(tmp_path: Path, shard_rows: list[int]) -> Path:
    """Build a real run directory whose shards hold the given row counts.

    The shards are written through C4's ``write_chunk_atomic`` and the
    manifest through the schema's own serialiser, so the result satisfies
    every public check: real NPZ files, real SHA-256 digests, a real
    ``manifest.json`` whose ``rows`` add up to ``total_points``.

    Args:
        tmp_path: Directory the run is created in.
        shard_rows: Number of rows per shard, in order.

    Returns:
        The run directory.
    """
    run_dir = tmp_path / "run"
    (run_dir / "raw").mkdir(parents=True)

    entries = []
    offset = 0
    for part_id, rows in enumerate(shard_rows):
        seq = np.arange(offset, offset + rows, dtype=np.int64)
        timestamp_ns = seq * (1_000_000_000 // SAMPLE_RATE_HZ)
        vibration = np.full(rows, 0.5, dtype=np.float64)
        temperature = np.full(rows, 35.0, dtype=np.float64)
        chunk = SimulationChunk(
            seq=seq,
            timestamp_ns=timestamp_ns,
            vibration=vibration,
            temperature=temperature,
        )
        entry = write_chunk_atomic(
            chunk,
            run_dir / "raw" / f"part-{part_id:06d}.npz",
            run_dir=run_dir,
            part_id=part_id,
        )
        entries.append(entry)
        offset += rows

    total_points = sum(shard_rows)
    manifest = Manifest(
        schema_version="1.0",
        run_id="c2_last_shard_bound_run",
        scenario_id=SCENARIO_ID,
        device_id=DEVICE_ID,
        seed=42,
        sample_rate_hz=SAMPLE_RATE_HZ,
        start_time_utc=START_TIME_UTC,
        total_points=total_points,
        units={"vibration": "g", "temperature": "degC"},
        status=ManifestStatus.COMPLETE,
        parts=tuple(entries),
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest_to_dict(manifest)), encoding="utf-8"
    )
    return run_dir


def _table_counts(database_path: Path) -> dict[str, int]:
    """Return the row count of every table that a consumption would fill."""
    connection = sqlite3.connect(str(database_path))
    try:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        ]
        return {
            name: int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in sorted(names)
        }
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the refusal
# --------------------------------------------------------------------------


def test_a_final_shard_longer_than_the_nominal_length_is_refused(
    tmp_path: Path,
) -> None:
    """A final shard of 256 rows after a 128-row shard is refused.

    This is the counter-example the public-API probe named: a run whose
    shards are cut ``[128, 256]`` puts twice the nominal number of samples
    in the final shard. The manifest itself is perfectly well formed -- the
    rows add up to ``total_points`` and every digest is real -- so only the
    consumer can catch it.
    """
    run_dir = _build_run(tmp_path, [128, 256])
    database_path = tmp_path / "g15.db"

    with pytest.raises(ConsumptionError) as excinfo:
        consume_run(run_dir, database_path, _consumer_config(tmp_path))

    message = str(excinfo.value)
    assert "part 1" in message
    assert "256" in message
    assert "128" in message
    assert "Nothing was written" in message


def test_the_refusal_names_the_part_the_rows_and_the_bound(tmp_path: Path) -> None:
    """The error names the part id, its row count and the inferred bound."""
    run_dir = _build_run(tmp_path, [128, 256])

    with pytest.raises(ConsumptionError) as excinfo:
        consume_run(run_dir, tmp_path / "g15.db", _consumer_config(tmp_path))

    message = str(excinfo.value)
    assert "part 1" in message
    assert "holds 256 rows" in message
    assert "exceeds the nominal shard length 128" in message


def test_a_refused_run_leaves_no_rows_behind(tmp_path: Path) -> None:
    """The refusal happens before any business write.

    Issue #69 requires that no ``runs`` / ``feature_windows`` / ``alarms`` /
    ``processed_chunks`` row survives the refusal. An empty database with the
    tables created is acceptable; leftover rows are not.
    """
    run_dir = _build_run(tmp_path, [128, 256])
    database_path = tmp_path / "g15.db"

    with pytest.raises(ConsumptionError):
        consume_run(run_dir, database_path, _consumer_config(tmp_path))

    counts = _table_counts(database_path)
    for name in (
        "runs",
        "feature_windows",
        "alarms",
        "processed_chunks",
        "alarm_states",
    ):
        assert counts.get(name, 0) == 0, f"{name} must not hold rows after a refusal"


def test_a_final_shard_far_longer_than_the_nominal_length_is_refused(
    tmp_path: Path,
) -> None:
    """The bound is checked against the inferred length, not a fixed number."""
    run_dir = _build_run(tmp_path, [64, 512])

    with pytest.raises(ConsumptionError) as excinfo:
        consume_run(run_dir, tmp_path / "g15.db", _consumer_config(tmp_path))

    message = str(excinfo.value)
    assert "512" in message
    assert "64" in message


# --------------------------------------------------------------------------
# the legal cases that must keep working
# --------------------------------------------------------------------------


def test_an_equal_length_final_shard_is_accepted(tmp_path: Path) -> None:
    """A final shard of exactly the nominal length is legal.

    This is the case the inclusive bound exists for: when ``total_points`` is
    an exact multiple of ``chunk_points`` the final shard holds exactly
    ``chunk_points`` rows. Refusing it would reject every evenly divisible
    run.
    """
    run_dir = _build_run(tmp_path, [128, 128])
    database_path = tmp_path / "g15.db"

    summary = consume_run(run_dir, database_path, _consumer_config(tmp_path))

    assert summary.shards_processed == 2
    assert summary.windows_written == 4
    assert summary.windows_dropped == 0

    counts = _table_counts(database_path)
    assert counts["feature_windows"] == 4
    assert counts["processed_chunks"] == 2


def test_an_equal_length_final_shard_over_three_shards_is_accepted(
    tmp_path: Path,
) -> None:
    """Three equal shards are legal as well."""
    run_dir = _build_run(tmp_path, [128, 128, 128])
    database_path = tmp_path / "g15.db"

    summary = consume_run(run_dir, database_path, _consumer_config(tmp_path))

    assert summary.shards_processed == 3
    assert summary.windows_written == 6
    assert summary.windows_dropped == 0


def test_a_short_final_shard_is_still_dropped_not_refused(tmp_path: Path) -> None:
    """A final shard shorter than the nominal length keeps the old behaviour.

    A short tail is legal. Its remainder is handled by the
    ``incomplete_window`` policy, which drops it; that is not a refusal.
    """
    run_dir = _build_run(tmp_path, [128, 100])
    database_path = tmp_path / "g15.db"

    summary = consume_run(run_dir, database_path, _consumer_config(tmp_path))

    assert summary.shards_processed == 2
    # 100 rows is one whole window of 64 plus 36 samples, which are dropped.
    assert summary.windows_written == 3
    assert summary.windows_dropped == 36


def test_a_short_final_shard_is_refused_under_the_error_policy(tmp_path: Path) -> None:
    """Under ``incomplete_window: error`` a short tail is refused instead."""
    run_dir = _build_run(tmp_path, [128, 100])
    database_path = tmp_path / "g15.db"

    with pytest.raises(ConsumptionError):
        consume_run(
            run_dir,
            database_path,
            _consumer_config(tmp_path, incomplete_window="error"),
        )


def test_a_single_shard_run_is_accepted(tmp_path: Path) -> None:
    """A single shard is the tail itself and has no bound to check.

    The manifest does not carry the nominal ``chunk_points``, so there is
    nothing to infer an upper bound from. The consumer must not invent one --
    a single-shard run of any length is legal.
    """
    run_dir = _build_run(tmp_path, [512])
    database_path = tmp_path / "g15.db"

    summary = consume_run(run_dir, database_path, _consumer_config(tmp_path))

    assert summary.shards_processed == 1
    assert summary.windows_written == 8
    assert summary.windows_dropped == 0


def test_a_single_shard_longer_than_a_two_shard_nominal_length_is_accepted(
    tmp_path: Path,
) -> None:
    """A single 512-row shard is legal even though 512 would be illegal as a tail.

    The bound comes from the *other* shards. With no other shard there is no
    inferred nominal length, so nothing is enforced -- which is the
    behaviour section 4 prescribes for the single-shard case.
    """
    run_dir = _build_run(tmp_path, [512])
    database_path = tmp_path / "g15.db"

    summary = consume_run(run_dir, database_path, _consumer_config(tmp_path))

    assert summary.shards_processed == 1
    assert summary.windows_written == 8


# --------------------------------------------------------------------------
# the boundary between the refusal and the divisibility rule
# --------------------------------------------------------------------------


def test_the_length_refusal_comes_before_the_divisibility_refusal(
    tmp_path: Path,
) -> None:
    """A run that breaks both rules names the tail, not the divisibility.

    The non-final shard here holds 100 rows, which is not a multiple of
    ``window_points=64``, *and* the final shard is longer than it. The
    length is the more fundamental violation -- it means the run was not cut
    as claimed -- so the error names the tail.
    """
    run_dir = _build_run(tmp_path, [100, 200])

    with pytest.raises(ConsumptionError) as excinfo:
        consume_run(run_dir, tmp_path / "g15.db", _consumer_config(tmp_path))

    message = str(excinfo.value)
    assert "part 1" in message
    assert "exceeds the nominal shard length 100" in message


def test_a_non_final_shard_that_is_not_a_whole_number_of_windows_is_refused(
    tmp_path: Path,
) -> None:
    """The pre-existing divisibility rule still holds."""
    run_dir = _build_run(tmp_path, [100, 100])

    with pytest.raises(ConsumptionError) as excinfo:
        consume_run(run_dir, tmp_path / "g15.db", _consumer_config(tmp_path))

    message = str(excinfo.value)
    assert "not a multiple of window_points" in message


def test_non_final_shards_of_differing_lengths_are_refused(tmp_path: Path) -> None:
    """The non-final shards must still agree on their length."""
    run_dir = _build_run(tmp_path, [128, 64, 64])

    with pytest.raises(ConsumptionError) as excinfo:
        consume_run(run_dir, tmp_path / "g15.db", _consumer_config(tmp_path))

    message = str(excinfo.value)
    assert "do not all have the same length" in message


# --------------------------------------------------------------------------
# the benchmark shape from section 8.1
# --------------------------------------------------------------------------


def test_the_acceptance_benchmark_shape_is_accepted(tmp_path: Path) -> None:
    """The shape section 8.1 names must keep working: 9 x 102400 + 78400.

    The final shard holds 78400 rows against a nominal length of 102400, so
    it is well inside the bound, and 78400 = 306 x 256 + 64 leaves 64 samples
    that the drop policy discards. This is the documented benchmark shape,
    checked here at a smaller scale with the same arithmetic.
    """
    run_dir = _build_run(tmp_path, [1024] * 9 + [784])
    database_path = tmp_path / "g15.db"

    summary = consume_run(run_dir, database_path, _consumer_config(tmp_path))

    assert summary.shards_processed == 10
    # 9 * 16 windows + 12 windows from 784 rows, with 16 samples dropped.
    assert summary.windows_written == 9 * 16 + 12
    assert summary.windows_dropped == 16
