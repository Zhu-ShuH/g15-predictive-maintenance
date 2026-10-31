"""Opt-in stress and repeatability tests for the whole pipeline (#51, owner B1).

What this file covers
---------------------
``docs/acceptance.md`` asks for the 1M-point production budget and for
fault-injection evidence, but neither of those says anything about what happens
when the same work is repeated.  This module measures that separately:

* repeated production of the same scenario must land byte-identical shards;
* repeated consumption of the same run must add no row after the first pass;
* three consumers racing on one run must never duplicate a row;
* a consumer stopped abruptly mid-pass must replay to the same stored content
  as a pass that was never interrupted.

What it deliberately does not claim
-----------------------------------
A pass on Windows that stops a child with :meth:`Popen.kill` is a
`TerminateProcess`, not a POSIX `SIGKILL`.  It proves the database survives an
abrupt death on this platform; it is **not** the Linux `kill -9` evidence
`docs/acceptance.md` section 4 requires, which stays in
`tests/fault_injection/test_pipeline_faults.py` and still needs a real Linux
host.  Likewise, nothing here measures peak RSS or the CLI wall clock.

Running it
----------
The stress round is opt-in because it starts child processes and repeats real
production several times, which is neither fast nor meaningful on a shared CI
runner.  Enable it with::

    G15_RUN_STRESS=1 python -m pytest -q -s tests/integration/test_stress_repeatability.py

Without the environment variable every test here is skipped, so the default
``python -m pytest -q` gate stays fast.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

import pytest

from g15.config import load_config, load_consumer_config
from g15.pipeline.consumer import consume_run
from g15.pipeline.producer import produce_run_detailed
from g15.storage.database import DatabaseError, connect

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPOSITORY_ROOT / "configs"
CONSUMER_CONFIG = CONFIGS / "consumer.yaml"

#: Controlled profile used by the repeat and concurrency rounds.
SMALL_PROFILE = "acceptance_spike_seed42"

#: Ten-shard profile used by the abrupt-stop round: a small run finishes before
#: the signal can land, and a case that cannot be interrupted proves nothing.
KILL_PROFILE = "benchmark_1m"

#: Rounds the repeat cases run before comparing.
ROUNDS = 3

#: Consumers that race on one run in the concurrency case.
RACERS = 3

_T = TypeVar("_T")

_ENV_FLAG = "G15_RUN_STRESS"

_TABLES = ("runs", "processed_chunks", "feature_windows", "alarms")


def _require_opt_in() -> None:
    """Skip the stress round unless the operator asked for a real measurement."""
    if os.environ.get(_ENV_FLAG) != "1":
        pytest.skip(
            f"stress round is opt-in; set {_ENV_FLAG}=1 to run it "
            "(it repeats production and starts child processes)"
        )


def _produce(tmp_path: Path, name: str, profile: str = SMALL_PROFILE) -> Path:
    """Produce one run of ``profile`` into its own directory.

    Args:
        tmp_path: Directory that receives the round.
        name: Sub-directory name, so rounds never share an output root.
        profile: Configuration file stem under ``configs/``.

    Returns:
        The published run directory.
    """
    directory = tmp_path / name
    directory.mkdir()
    config = load_config(CONFIGS / f"{profile}.yaml")
    return produce_run_detailed(config, directory).run_dir


def _digests(run_dir: Path) -> list[tuple[int, int, str]]:
    """Return ``(part_id, rows, sha256)`` for every shard the manifest names."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return [
        (int(part["part_id"]), int(part["rows"]), str(part["sha256"]))
        for part in manifest["parts"]
    ]


def _retry_while_recovering(
    database: Path, operation: Callable[[], _T], deadline_seconds: float = 60.0
) -> _T:
    """Run a read against a database a child may have left mid-write.

    A process stopped while it holds the write lock leaves its WAL behind, and
    the next ``connect`` can fail with ``disk I/O error`` until SQLite has
    rolled it back -- sometimes on the open, sometimes on the first statement.
    That window belongs to the platform, not to the consumer, so the test waits
    it out instead of reporting a false failure.
    """
    deadline = time.monotonic() + deadline_seconds
    while True:
        try:
            return operation()
        except (DatabaseError, sqlite3.Error) as exc:
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"database {database} did not become readable within "
                    f"{deadline_seconds}s after the child was stopped: {exc}"
                ) from exc
            time.sleep(0.05)


def _counts(database: Path) -> dict[str, int]:
    """Return the row count of every table the consumer writes."""
    return _retry_while_recovering(database, lambda: _read_counts(database))


def _read_counts(database: Path) -> dict[str, int]:
    """Read the row counts once; ``_counts`` retries this while WAL recovers."""
    connection = connect(database)
    try:
        return {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in _TABLES
        }
    finally:
        connection.close()


def _snapshot(database: Path) -> dict[str, Any]:
    """Return the comparable content of one consumed database, table by table.

    Counting rows would accept a replay that wrote the right number of wrong
    rows, so the abrupt-stop case compares the stored values instead.
    """
    return _retry_while_recovering(database, lambda: _read_snapshot(database))


def _read_snapshot(database: Path) -> dict[str, Any]:
    """Read the stored content once; ``_snapshot`` retries this on WAL recovery."""
    queries = {
        "runs": (
            "SELECT run_id, scenario_id, schema_version, status "
            "FROM runs ORDER BY run_id"
        ),
        "processed_chunks": "SELECT part_id, sha256 FROM processed_chunks ORDER BY part_id",
        "feature_windows": (
            "SELECT device_id, start_seq, end_seq_exclusive, features_json, "
            "health_score FROM feature_windows ORDER BY device_id, start_seq"
        ),
        "alarms": (
            "SELECT device_id, rule_id, start_seq, level, reason FROM alarms "
            "ORDER BY device_id, rule_id, start_seq"
        ),
    }
    connection = connect(database)
    try:
        return {
            name: [tuple(row) for row in connection.execute(sql).fetchall()]
            for name, sql in queries.items()
        }
    finally:
        connection.close()


def _consume_until_stopped(run_dir: str, database: str, config_path: str) -> None:
    """Run one consumption in a child that the parent may kill mid-pass.

    Kept separate from :func:`_consume_in_child` because this one must stay
    silent: anything it prints would be lost when the process dies.
    """
    consume_run(
        Path(run_dir),
        Path(database),
        load_consumer_config(Path(config_path)),
    )


def _consume_in_child(
    run_dir: str, database: str, config_path: str, queue: Any
) -> None:
    """Consume one run inside a spawned child and report the outcome.

    Spawn is used on purpose: the racers must be separate interpreters, or the
    case would only prove that one process can re-enter its own code.
    """
    try:
        summary = consume_run(
            Path(run_dir),
            Path(database),
            load_consumer_config(Path(config_path)),
        )
    except Exception as exc:  # pylint: disable=broad-except
        queue.put(("failed", type(exc).__name__, 0))
    else:
        queue.put(("written", "ok", summary.windows_written))


def test_repeated_production_lands_byte_identical_shards(tmp_path: Path) -> None:
    """Three productions of one scenario must publish the same shard digests.

    Determinism is what makes a replay comparison meaningful: if two fresh runs
    of the same configuration disagreed, the idempotency cases below would be
    comparing noise.
    """
    _require_opt_in()
    rounds: list[list[tuple[int, int, str]]] = []
    elapsed: list[float] = []
    for index in range(ROUNDS):
        started = time.perf_counter()
        run_dir = _produce(tmp_path, f"round{index}")
        elapsed.append(time.perf_counter() - started)
        rounds.append(_digests(run_dir))

    assert all(round_ == rounds[0] for round_ in rounds[1:]), (
        "repeating the same scenario must publish identical shard digests; "
        f"got {[len(item) for item in rounds]} shards, "
        f"first digests {rounds[0][:1]} vs {rounds[-1][:1]}"
    )
    # The timings are evidence for the report, not a gate: this host is not a
    # valid measurement machine and no budget is asserted here.
    print(f"repeated production seconds: {[round(value, 4) for value in elapsed]}")


def test_repeated_consumption_adds_no_row(tmp_path: Path) -> None:
    """Re-consuming an untouched run must leave every table's count unchanged."""
    _require_opt_in()
    run_dir = _produce(tmp_path, "replay")
    database = run_dir / "artifacts" / "results.sqlite"
    config = load_consumer_config(CONSUMER_CONFIG)

    first = consume_run(run_dir, database, config)
    baseline = _counts(database)
    assert baseline["feature_windows"] == first.windows_written

    for round_index in range(ROUNDS - 1):
        again = consume_run(run_dir, database, config)
        assert again.windows_written == 0, f"round {round_index} wrote windows"
        assert again.alarms_written == 0, f"round {round_index} wrote alarms"
        assert again.shards_processed == 0, f"round {round_index} re-analysed a shard"
        assert again.shards_skipped == first.shards_processed
        assert _counts(database) == baseline, f"round {round_index} changed the tables"


def test_racing_consumers_never_duplicate_rows(tmp_path: Path) -> None:
    """Three consumers on one run must agree on a single stored result.

    Concurrency is not a contracted capability of ``consume_run``; the point of
    this case is the invariant that matters either way -- whichever consumer
    wins, the database must end up with exactly the rows one pass would have
    written, with no duplicated shard and no half-written window.
    """
    _require_opt_in()
    run_dir = _produce(tmp_path, "racing")
    database = run_dir / "artifacts" / "results.sqlite"

    reference_dir = _produce(tmp_path, "reference")
    reference_db = reference_dir / "artifacts" / "results.sqlite"
    consume_run(reference_dir, reference_db, load_consumer_config(CONSUMER_CONFIG))

    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_consume_in_child,
            args=(str(run_dir), str(database), str(CONSUMER_CONFIG), queue),
        )
        for _ in range(RACERS)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(180)

    outcomes = []
    while not queue.empty():
        outcomes.append(queue.get())
    assert len(outcomes) == RACERS, f"only {len(outcomes)} consumers reported back"

    written = sum(windows for status, _, windows in outcomes if status == "written")
    assert written == _counts(reference_db)["feature_windows"], (
        "the consumers together must write exactly one pass worth of windows; "
        f"outcomes were {outcomes}"
    )
    assert _counts(database) == _counts(reference_db), (
        "the raced database must match a single pass; "
        f"raced {_counts(database)} vs single {_counts(reference_db)}"
    )
    print(f"racing consumer outcomes: {outcomes}")


def test_abruptly_stopped_consumption_replays_to_the_same_content(
    tmp_path: Path,
) -> None:
    """A consumer killed mid-pass must replay to an uninterrupted run's content.

    The child is a separate interpreter running the public ``consume_run``
    entry, not an in-process call: a stop has to take the process down, so it
    cannot be simulated from inside the test.  The kill lands only after the
    first shard transaction has committed and before the last one, so the
    database really is a partial prefix and the replay below is exercising
    recovery rather than a run that never started.

    On Windows the stop is ``TerminateProcess``, which is not POSIX `SIGKILL`;
    this is supplementary evidence for the same recovery property, not a
    substitute for the Linux case in ``tests/fault_injection/``.
    """
    _require_opt_in()
    run_dir = _produce(tmp_path, "killed", profile=KILL_PROFILE)
    database = run_dir / "artifacts" / "results.sqlite"

    reference_dir = tmp_path / "reference"
    shutil.copytree(run_dir, reference_dir)
    reference_db = reference_dir / "artifacts" / "results.sqlite"
    consume_run(reference_dir, reference_db, load_consumer_config(CONSUMER_CONFIG))

    total_parts = len(_digests(run_dir))
    context = multiprocessing.get_context("spawn")
    process = context.Process(  # pylint: disable=consider-using-with
        target=_consume_until_stopped,
        args=(str(run_dir), str(database), str(CONSUMER_CONFIG)),
    )
    process.start()
    try:
        deadline = time.monotonic() + 120.0
        committed = 0
        while committed < 1:
            if not process.is_alive():
                pytest.fail(
                    "the consumer finished before it could be stopped; "
                    f"exit code {process.exitcode}"
                )
            if time.monotonic() > deadline:
                pytest.fail("the consumer never committed its first shard")
            try:
                connection = connect(database)
                try:
                    row = connection.execute(
                        "SELECT COUNT(*) FROM processed_chunks"
                    ).fetchone()
                finally:
                    connection.close()
                committed = 0 if row is None else int(row[0])
            except Exception:  # pylint: disable=broad-except
                committed = 0
            time.sleep(0.002)
        process.kill()
        process.join(60)
        returncode = 0 if process.exitcode is None else process.exitcode
    finally:
        if process.is_alive():  # pragma: no cover - defensive
            process.kill()
            process.join(60)

    assert returncode != 0, "a stopped consumer must not report success"
    partial = _counts(database)
    assert 1 <= partial["processed_chunks"] < total_parts, (
        "the stop must land after a committed shard and before the last one; "
        f"got {partial['processed_chunks']} of {total_parts} shards"
    )

    replay = consume_run(run_dir, database, load_consumer_config(CONSUMER_CONFIG))
    final = _counts(database)
    # Evidence for the report: how far the stop landed and what the replay had
    # to finish.  Nothing here is asserted except the content comparison below.
    print(
        "abrupt stop: "
        f"{partial['processed_chunks']} of {total_parts} shards committed, "
        f"replay wrote {replay.windows_written} windows / "
        f"{replay.alarms_written} alarms, processed {replay.shards_processed} "
        f"and skipped {replay.shards_skipped}; final {final}"
    )
    assert _snapshot(database) == _snapshot(
        reference_db
    ), "the replay after an abrupt stop must reproduce the uninterrupted run"
