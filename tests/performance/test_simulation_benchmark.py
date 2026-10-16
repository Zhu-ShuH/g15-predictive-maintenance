"""Million-point benchmark for the G15 simulation kernel (Issue #5, owner B1).

What this file measures
-----------------------
``docs/interface.md`` section 7.3 defines the acceptance measurement as: one run
of 1,000,000 points, timed with :func:`time.perf_counter` from the moment the
simulation starts until **every NPZ shard and the complete Manifest have landed
on disk**, with ``elapsed < 60 s``.

The Manifest and the shard writer are owned by C4 (``pipeline/serialization.py``,
Issue #3/#11) and are not implemented yet, so this file reports two figures:

* ``generation``  - the simulation kernel alone: trend + noise for all four
  arrays of every shard.  This is the number B1 is responsible for.
* ``generation+npz`` - the same plus writing every shard with
  :func:`numpy.savez` into a temporary directory.  This approximates the I/O
  cost but **is not** the contractual figure: it uses NumPy's own writer, not
  the project's atomic shard writer, and it writes no Manifest and no SHA-256.

Neither figure is the final acceptance result.  The final number must be
re-measured through ``produce_run`` once C4 lands, on the target Linux hosts
(Ubuntu 24.04 and RHEL 9).  Any report quoting these numbers must say so.

Running it
----------
The benchmark is opt-in because a shared CI runner is not a valid measurement
host (``docs/interface.md`` section 7.3).  Enable it with::

    G15_RUN_BENCHMARK=1 .venv/Scripts/python.exe -m pytest \
        -q -s tests/performance/test_simulation_benchmark.py

Without the environment variable every test here is skipped, so the default
``python -m pytest -q`` gate stays fast and CI stays honest.
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from g15.simulation.engine import (
    SimulationChunk,
    load_scenario_parameters,
    simulate_chunk,
)

#: Total points of the contractual benchmark run.
TOTAL_POINTS = 1_000_000

#: Shard size from the reference configuration (``configs/benchmark_1m.yaml``).
CHUNK_POINTS = 102_400

#: Contractual wall-clock budget in seconds (``docs/interface.md`` section 7.3).
BUDGET_SECONDS = 60.0

_ENV_FLAG = "G15_RUN_BENCHMARK"


def _require_opt_in() -> None:
    """Skip the benchmark unless the operator asked for a real measurement."""
    if os.environ.get(_ENV_FLAG) != "1":
        pytest.skip(
            f"performance benchmark is opt-in; set {_ENV_FLAG}=1 to measure "
            "(a shared CI runner is not a valid measurement host)"
        )


def _scenario(**scenario_overrides: object) -> dict:
    """Return the 1M-point benchmark scenario from ``docs/interface.md`` section 4."""
    scenario = {
        "id": "benchmark_1m",
        "seed": 42,
        "start_time_utc": "2026-10-14T00:00:00Z",
        "device_id": "DEV-001",
        "sample_rate_hz": 100,
        "total_points": TOTAL_POINTS,
        "chunk_points": CHUNK_POINTS,
    }
    scenario.update(scenario_overrides)
    return {
        "schema_version": "1.0",
        "scenario": scenario,
        "signals": {
            "vibration": {
                "baseline": 0.5,
                "degradation_rate_per_sample": 0.000001,
                "noise_std": 0.05,
                "unit": "g",
            },
            "temperature": {
                "baseline": 35.0,
                "degradation_rate_per_sample": 0.000003,
                "noise_std": 0.15,
                "unit": "degC",
            },
        },
    }


def _shard_bounds(total: int, chunk: int) -> list[tuple[int, int]]:
    """Return ``[(start, count), ...]`` covering ``total`` points in ``chunk`` shards."""
    bounds: list[tuple[int, int]] = []
    start = 0
    while start < total:
        count = min(chunk, total - start)
        bounds.append((start, count))
        start += count
    return bounds


def _machine_facts() -> dict[str, str]:
    """Return the host facts that ``docs/interface.md`` section 7.3 requires."""
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": platform.processor() or platform.machine(),
        "cpu_count": str(os.cpu_count()),
        "python": sys.version.split()[0],
        "numpy": np.__version__,
    }


def _run_generation(config: dict) -> tuple[float, int, int]:
    """Generate every shard of the run; return ``(seconds, points, scalars)``."""
    params = load_scenario_parameters(config)
    chunk_points = int(config["scenario"]["chunk_points"])  # type: ignore[index]
    start = time.perf_counter()
    points = 0
    scalars = 0
    for start_seq, count in _shard_bounds(params.total_points, chunk_points):
        chunk = simulate_chunk(config, start_seq, count)
        points += int(chunk.seq.shape[0])
        scalars += int(chunk.vibration.shape[0]) + int(chunk.temperature.shape[0])
    return time.perf_counter() - start, points, scalars


def _run_generation_and_npz(config: dict, directory: Path) -> tuple[float, int]:
    """Generate every shard and write it with :func:`numpy.savez`.

    Returns:
        ``(seconds, bytes_written)``.
    """
    params = load_scenario_parameters(config)
    chunk_points = int(config["scenario"]["chunk_points"])  # type: ignore[index]
    written = 0
    start = time.perf_counter()
    for index, (start_seq, count) in enumerate(
        _shard_bounds(params.total_points, chunk_points)
    ):
        chunk = simulate_chunk(config, start_seq, count)
        path = directory / f"part-{index:06d}.npz"
        np.savez(
            path,
            seq=chunk.seq,
            timestamp_ns=chunk.timestamp_ns,
            vibration=chunk.vibration,
            temperature=chunk.temperature,
        )
        written += path.stat().st_size
    return time.perf_counter() - start, written


def _report(title: str, seconds: float, facts: dict[str, str], **extra: object) -> None:
    """Print a benchmark record in the shape ``docs/interface.md`` 7.3 asks for."""
    print(f"\n=== {title} ===")
    for key, value in facts.items():
        print(f"{key:>12}: {value}")
    for key, value in extra.items():
        print(f"{key:>12}: {value}")
    print(f"{'elapsed_s':>12}: {seconds:.3f}")
    print(f"{'budget_s':>12}: {BUDGET_SECONDS:.1f}")
    print(f"{'verdict':>12}: {'PASS' if seconds < BUDGET_SECONDS else 'FAIL'}")


def test_benchmark_generation_one_million_points() -> None:
    """The simulation kernel must generate 1,000,000 points well inside the budget.

    This is the algorithm-only figure.  It is **not** the contractual acceptance
    result, which additionally requires the shard writer and the Manifest.
    """
    _require_opt_in()
    config = _scenario()

    seconds, points, scalars = _run_generation(config)

    _report(
        "simulation kernel only (NOT the contractual figure)",
        seconds,
        _machine_facts(),
        points=points,
        scalar_values=scalars,
        chunk_points=CHUNK_POINTS,
        scope="trend + noise; no serialisation, no Manifest",
    )
    assert points == TOTAL_POINTS
    assert seconds < BUDGET_SECONDS, f"generation took {seconds:.1f}s"


def test_benchmark_generation_and_npz_write() -> None:
    """Generate 1,000,000 points and write every shard to disk.

    Uses :func:`numpy.savez` rather than the project's atomic writer, so the
    figure approximates the I/O cost without being the acceptance measurement.
    """
    _require_opt_in()
    config = _scenario()

    with tempfile.TemporaryDirectory(prefix="g15-bench-") as tmp:
        directory = Path(tmp)
        seconds, written = _run_generation_and_npz(config, directory)
        shards = len(list(directory.glob("part-*.npz")))

    _report(
        "simulation kernel + np.savez shards (approximation, no Manifest)",
        seconds,
        _machine_facts(),
        points=TOTAL_POINTS,
        chunk_points=CHUNK_POINTS,
        shards=shards,
        bytes_written=written,
        scope="NumPy writer, not the project's atomic writer; no Manifest/SHA-256",
    )
    assert shards == len(_shard_bounds(TOTAL_POINTS, CHUNK_POINTS))


@pytest.mark.parametrize("chunk_points", [65_536, 102_400, 250_000, 1_000_000])
def test_million_point_result_is_independent_of_chunk_size(chunk_points: int) -> None:
    """Sharding must not change a single generated value, at full run length.

    This guards the property the producer depends on: a crashed and restarted
    run, or a different ``chunk_points``, must reproduce byte-identical data.
    """
    _require_opt_in()
    previous: SimulationChunk | None = None
    for start_seq, count in _shard_bounds(TOTAL_POINTS, chunk_points):
        chunk = simulate_chunk(_scenario(), start_seq, count)
        if previous is not None:
            assert chunk.seq[0] == previous.seq[-1] + 1
        previous = chunk

    one_shot = simulate_chunk(_scenario(chunk_points=chunk_points), 0, TOTAL_POINTS)
    probe = simulate_chunk(_scenario(), 999_000, 1_000)

    assert np.array_equal(one_shot.vibration[999_000:], probe.vibration)
    assert np.array_equal(one_shot.temperature[999_000:], probe.temperature)
    assert np.array_equal(one_shot.timestamp_ns[999_000:], probe.timestamp_ns)
