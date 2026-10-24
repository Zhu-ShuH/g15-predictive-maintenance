"""Unit tests for the run producer (Issue #11, owner C4).

The producer is the bridge between the simulation kernel and the on-disk
contract, so the tests pin three things:

* the shard layout and the manifest that describes it,
* determinism (same configuration, same numbers, different ``run_id``),
* the failure rule that matters most: **a failed run must never publish a
  ``complete`` manifest**.

Most fixtures are plain mappings; compatibility cases also exercise C3-shaped
objects. The real-loader integration runs when Issue #4's ``load_config`` is
available, and otherwise reports an explicit skip rather than substituting a
fake loader. The loader and source YAML remain owned by Issue #4.
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import yaml

from g15.pipeline import producer
from g15.pipeline.schema import ManifestStatus, manifest_from_dict
from g15.pipeline.serialization import ChunkIOError, read_chunk
from g15.pipeline.producer import (
    ProductionSummary,
    make_run_id,
    produce_run,
    produce_run_detailed,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

RUN_ID_PATTERN = re.compile(r"^(\d{8}T\d{6}Z)-([A-Za-z0-9._-]+)-([0-9a-f]{6})$")


def scenario_config(
    total_points: int = 1000,
    chunk_points: int = 256,
    seed: int = 42,
    events: list[dict[str, object]] | None = None,
    vibration_unit: str = "g",
    temperature_unit: str = "degC",
    start_time_utc: str = "2026-10-14T00:00:00Z",
) -> dict[str, object]:
    """Return a configuration shaped like ``docs/interface.md`` section 4."""
    return {
        "schema_version": "1.0",
        "scenario": {
            "id": "bearing_001",
            "seed": seed,
            "start_time_utc": start_time_utc,
            "device_id": "DEV-001",
            "sample_rate_hz": 100,
            "total_points": total_points,
            "chunk_points": chunk_points,
        },
        "signals": {
            "vibration": {
                "baseline": 0.5,
                "degradation_rate_per_sample": 1e-06,
                "noise_std": 0.05,
                "unit": vibration_unit,
            },
            "temperature": {
                "baseline": 35.0,
                "degradation_rate_per_sample": 3e-06,
                "noise_std": 0.15,
                "unit": temperature_unit,
            },
        },
        "fault_generation": {
            "mode": "manual",
            "stochastic_probability_per_window": 0.0,
            "events": events if events is not None else [],
        },
    }


def single_run_dir(output_root: Path) -> Path:
    """Return the only run directory created under ``output_root``."""
    candidates = [item for item in output_root.iterdir() if item.is_dir()]
    assert len(candidates) == 1, f"expected one run directory, got {candidates}"
    return candidates[0]


def load_manifest(run_dir: Path):
    """Parse the published manifest, failing the test if it is absent."""
    payload = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return payload, manifest_from_dict(payload)


# --------------------------------------------------------------------------- #
# Shard layout and manifest
# --------------------------------------------------------------------------- #


def test_run_publishes_a_complete_manifest(tmp_path: Path) -> None:
    """A finished run must describe every shard and add up exactly."""
    run_dir = produce_run(scenario_config(total_points=1000), tmp_path)

    payload, manifest = load_manifest(run_dir)

    assert manifest.status is ManifestStatus.COMPLETE
    assert payload["schema_version"] == "1.0"
    assert manifest.total_points == 1000
    assert [entry.rows for entry in manifest.parts] == [256, 256, 256, 232]
    assert sum(entry.rows for entry in manifest.parts) == 1000
    assert [entry.path for entry in manifest.parts] == [
        "raw/part-000000.npz",
        "raw/part-000001.npz",
        "raw/part-000002.npz",
        "raw/part-000003.npz",
    ]


def test_exact_division_publishes_no_empty_tail(tmp_path: Path) -> None:
    """1024 / 256 must produce four full shards, not a fifth empty one."""
    run_dir = produce_run(
        scenario_config(total_points=1024, chunk_points=256), tmp_path
    )

    _, manifest = load_manifest(run_dir)

    assert len(manifest.parts) == 4
    assert {entry.rows for entry in manifest.parts} == {256}
    assert len(list((run_dir / "raw").glob("part-*.npz"))) == 4


def test_shorter_than_one_chunk_produces_a_single_shard(tmp_path: Path) -> None:
    """A run smaller than ``chunk_points`` is one short shard."""
    run_dir = produce_run(scenario_config(total_points=100, chunk_points=256), tmp_path)

    _, manifest = load_manifest(run_dir)

    assert len(manifest.parts) == 1
    assert manifest.parts[0].rows == 100


def test_non_divisible_length_ends_with_one_short_tail(tmp_path: Path) -> None:
    """1000 / 256 is documented as four shards: 256, 256, 256, 232.

    ``docs/interface.md`` section 5.1 says the tail is a short shard, never
    padded, never merged, never dropped.  This pins the closed form from the
    module docstring so a future "round up to full chunks" change fails here.
    """
    run_dir = produce_run(
        scenario_config(total_points=1000, chunk_points=256), tmp_path
    )

    _, manifest = load_manifest(run_dir)

    assert [entry.part_id for entry in manifest.parts] == [0, 1, 2, 3]
    assert [entry.rows for entry in manifest.parts] == [256, 256, 256, 232]
    assert sum(entry.rows for entry in manifest.parts) == 1000
    # Only the last shard is short, and it is short by the exact remainder.
    assert all(entry.rows == 256 for entry in manifest.parts[:-1])
    assert manifest.parts[-1].rows == 1000 % 256

    tail = manifest.parts[-1]
    chunk = read_chunk(run_dir / tail.path, tail)
    # Real rows, no padding: the tail ends on the run's last sample.
    assert chunk.vibration.shape == (232,)
    assert int(chunk.seq[-1]) == 999


def test_the_last_shard_bound_is_inclusive_of_chunk_points(tmp_path: Path) -> None:
    """The last shard's row count is ``<= chunk_points``, not ``<``.

    ``docs/interface.md`` section 8.1 states the closed form
    ``rows = total_points - (n - 1) * chunk_points`` and derives a bound on
    it.  When ``total_points`` is an exact multiple of ``chunk_points`` the
    ``ceil`` does not advance, so the last shard holds exactly
    ``chunk_points`` rows -- the bound is inclusive.  A consumer that checks
    the last shard with ``<`` would reject a legitimate run, so the bound is
    pinned here.

    Both the multi-shard and the single-shard exact-division cases are
    covered: ``512 / 256`` ends on a two-row tail equal to the nominal size,
    and ``256 / 256`` is a single shard of exactly one chunk.
    """
    run_dir = produce_run(scenario_config(total_points=512, chunk_points=256), tmp_path)

    _, manifest = load_manifest(run_dir)

    assert [entry.rows for entry in manifest.parts] == [256, 256]
    assert manifest.parts[-1].rows == 256
    # The bound holds: the last shard is never larger than the nominal size.
    assert manifest.parts[-1].rows <= 256
    # ...and it is exactly equal here, which is the case a ``<`` check breaks.
    assert manifest.parts[-1].rows == manifest.parts[0].rows

    single_dir = produce_run(
        scenario_config(total_points=256, chunk_points=256), tmp_path
    )

    _, single_manifest = load_manifest(single_dir)

    assert len(single_manifest.parts) == 1
    assert single_manifest.parts[0].rows == 256
    assert single_manifest.parts[0].rows <= 256


def test_shards_are_readable_and_contiguous(tmp_path: Path) -> None:
    """Each shard must verify against its entry and continue the previous one."""
    run_dir = produce_run(
        scenario_config(total_points=1000, chunk_points=256), tmp_path
    )
    _, manifest = load_manifest(run_dir)

    seen: list[int] = []
    for entry in manifest.parts:
        chunk = read_chunk(run_dir / entry.path, entry)
        assert int(chunk.seq[0]) == (0 if not seen else seen[-1] + 1)
        seen.extend(int(value) for value in chunk.seq)

    assert seen == list(range(1000))


def test_manifest_records_scenario_metadata(tmp_path: Path) -> None:
    """Identifiers and units must be copied from the configuration."""
    run_dir = produce_run(
        scenario_config(total_points=300, chunk_points=256),
        tmp_path,
    )

    payload, manifest = load_manifest(run_dir)

    assert manifest.scenario_id == "bearing_001"
    assert manifest.device_id == "DEV-001"
    assert manifest.seed == 42
    assert manifest.sample_rate_hz == 100
    assert manifest.start_time_utc == "2026-10-14T00:00:00Z"
    assert payload["units"] == {"vibration": "g", "temperature": "degC"}


@pytest.mark.parametrize(
    ("keyword", "value"),
    [("vibration_unit", "m/s2"), ("temperature_unit", "K"), ("vibration_unit", "")],
)
def test_non_contract_unit_publishes_nothing(
    tmp_path: Path, keyword: str, value: str
) -> None:
    """Section 5.2 freezes the units; producer must not relabel the data.

    The check runs before the run directory is created, so a rejected unit
    leaves no shards behind for a consumer to pick up.
    """
    config = scenario_config(total_points=32, chunk_points=16, **{keyword: value})
    output_root = tmp_path / "runs"

    with pytest.raises(ValueError, match="section 5.2"):
        produce_run_detailed(config, output_root)

    assert not output_root.exists()
    assert list(tmp_path.rglob("manifest.json")) == []


def test_run_id_is_unique_and_filesystem_safe(tmp_path: Path) -> None:
    """Two runs of the same scenario must not collide."""
    first = produce_run(scenario_config(total_points=256), tmp_path)
    second = produce_run(scenario_config(total_points=256), tmp_path)

    assert first.name != second.name
    for run_dir in (first, second):
        match = RUN_ID_PATTERN.match(run_dir.name)
        assert match, run_dir.name
        assert match.group(2) == "bearing_001"


def test_make_run_id_sanitises_the_scenario_slug() -> None:
    """Slug characters outside ``[A-Za-z0-9._-]`` must not reach the path."""
    run_id = make_run_id("轴承 001/失败")

    assert RUN_ID_PATTERN.match(run_id)
    assert "/" not in run_id
    assert " " not in run_id


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_same_config_gives_identical_numbers(tmp_path: Path) -> None:
    """Same seed must reproduce the data; only ``run_id`` may differ."""
    first = produce_run(scenario_config(total_points=1000), tmp_path)
    second = produce_run(scenario_config(total_points=1000), tmp_path)

    _, manifest_a = load_manifest(first)
    _, manifest_b = load_manifest(second)
    assert [entry.rows for entry in manifest_a.parts] == [
        entry.rows for entry in manifest_b.parts
    ]

    for entry_a, entry_b in zip(manifest_a.parts, manifest_b.parts, strict=True):
        chunk_a = read_chunk(first / entry_a.path, entry_a)
        chunk_b = read_chunk(second / entry_b.path, entry_b)
        for name in ("seq", "timestamp_ns", "vibration", "temperature"):
            assert np.array_equal(getattr(chunk_a, name), getattr(chunk_b, name))


def test_different_seed_changes_the_noise(tmp_path: Path) -> None:
    """The seed must actually reach the generator."""
    first = produce_run(scenario_config(total_points=256, seed=42), tmp_path)
    second = produce_run(scenario_config(total_points=256, seed=7), tmp_path)

    _, manifest_a = load_manifest(first)
    _, manifest_b = load_manifest(second)
    chunk_a = read_chunk(first / manifest_a.parts[0].path, manifest_a.parts[0])
    chunk_b = read_chunk(second / manifest_b.parts[0].path, manifest_b.parts[0])

    assert not np.array_equal(chunk_a.vibration, chunk_b.vibration)


# --------------------------------------------------------------------------- #
# Truth file and the Issue #6 hook
# --------------------------------------------------------------------------- #


def test_no_truth_file_without_events(tmp_path: Path) -> None:
    """An event-free run must not fabricate an empty answer key."""
    run_dir = produce_run(scenario_config(total_points=256), tmp_path)

    assert not (run_dir / "private_truth" / "faults.json").exists()


def test_truth_file_is_written_when_the_hook_reports_events(
    tmp_path: Path, monkeypatch
) -> None:
    """The producer must persist the answer key without reading it back."""
    module = types.ModuleType("g15.simulation.faults")

    class FakeEvent:
        """Stand-in for :class:`g15.simulation.faults.FaultEvent`."""

        event_id = "F001"
        start_seq = 10
        end_seq_exclusive = 20
        amplitude = 5.0

        def to_dict(self) -> dict[str, object]:
            """Return the section 5.3 truth record."""
            return {
                "event_id": self.event_id,
                "device_id": "DEV-001",
                "type": "spike",
                "start_seq": self.start_seq,
                "end_seq_exclusive": self.end_seq_exclusive,
                "amplitude": self.amplitude,
            }

    def fake_inject(chunk, config, rng=None):  # noqa: ANN001, ARG001
        """Return the chunk untouched plus one event, twice for the same id."""
        return chunk, [FakeEvent(), FakeEvent()]

    module.inject_faults = fake_inject  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "g15.simulation.faults", module)

    summary = produce_run_detailed(scenario_config(total_points=256), tmp_path)

    truth = json.loads(
        (summary.run_dir / "private_truth" / "faults.json").read_text(encoding="utf-8")
    )
    assert summary.fault_events == 1, "duplicate event_id must be collapsed"
    assert len(truth["events"]) == 1
    assert truth["events"][0]["event_id"] == "F001"
    assert truth["run_id"] == summary.run_dir.name


def test_event_free_runs_without_the_hook_still_succeed(
    tmp_path: Path, monkeypatch
) -> None:
    """Only event-free runs may proceed when Issue #6 is unavailable."""
    monkeypatch.setitem(
        sys.modules, "g15.simulation.faults", types.ModuleType("g15.simulation.faults")
    )
    summary = produce_run_detailed(scenario_config(total_points=256), tmp_path)

    assert summary.fault_events == 0
    assert (summary.run_dir / "manifest.json").is_file()


@pytest.mark.parametrize("mode", ["manual", "stochastic", "combined"])
def test_requested_faults_without_hook_publish_nothing(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    """Missing injection must not silently turn requested faults into healthy data."""
    monkeypatch.setitem(
        sys.modules, "g15.simulation.faults", types.ModuleType("g15.simulation.faults")
    )
    config = scenario_config(total_points=32, chunk_points=16)
    block = config["fault_generation"]
    block["mode"] = mode
    if mode in ("manual", "combined"):
        block["events"] = [
            {
                "event_id": "F001",
                "type": "spike",
                "start_seq": 4,
                "duration_points": 4,
                "amplitude": 5.0,
            }
        ]
    if mode in ("stochastic", "combined"):
        block["stochastic_probability_per_window"] = 0.25
    output_root = tmp_path / "runs"
    with pytest.raises(RuntimeError, match="requires fault injection"):
        produce_run_detailed(config, output_root)
    assert not output_root.exists()
    assert list(tmp_path.rglob("manifest.json")) == []


@pytest.mark.parametrize(
    "representation", ["nested_mapping", "nested_object", "flat_mapping", "flat_object"]
)
def test_scenario_id_alias_preserves_manifest_and_run_id(
    tmp_path: Path, representation: str
) -> None:
    """Both mappings and C3-shaped objects retain their declared scenario id."""
    config = scenario_config(total_points=32, chunk_points=16)
    config["scenario"]["scenario_id"] = config["scenario"].pop("id")
    if representation.startswith("flat"):
        scenario = config.pop("scenario")
        config.update(scenario)
    elif representation == "nested_object":
        config["scenario"] = types.SimpleNamespace(**config["scenario"])
    if representation.endswith("object"):
        config = types.SimpleNamespace(**config)
    run_dir = produce_run(config, tmp_path)
    payload, manifest = load_manifest(run_dir)
    assert payload["scenario_id"] == manifest.scenario_id == "bearing_001"
    assert "-bearing_001-" in run_dir.name


def test_real_c3_loader_produces_a_complete_event_free_run(tmp_path: Path) -> None:
    """Exercise the actual loader when Issue #4 is present in the integration tree."""
    from g15 import config as config_module  # pylint: disable=import-outside-toplevel

    if not callable(getattr(config_module, "load_config", None)):
        pytest.skip("Issue #4 load_config is not present on this PR base")
    config_path = tmp_path / "scenario.yaml"
    config_path.write_text(
        yaml.safe_dump(scenario_config(total_points=32, chunk_points=16)),
        encoding="utf-8",
    )
    config = config_module.load_config(config_path)
    assert config.scenario.scenario_id == "bearing_001"
    summary = produce_run_detailed(config, tmp_path / "runs")
    payload, manifest = load_manifest(summary.run_dir)
    assert payload["scenario_id"] == "bearing_001"
    assert manifest.total_points == 32
    assert [part.rows for part in manifest.parts] == [16, 16]
    assert summary.fault_events == 0
    assert "-bearing_001-" in summary.run_dir.name


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


def test_failed_run_publishes_no_complete_manifest(tmp_path: Path, monkeypatch) -> None:
    """A write failure must leave the run unconsumable, not half-valid."""
    real_write = producer.write_chunk_atomic
    calls = {"count": 0}

    def flaky(chunk, path, **kwargs):  # noqa: ANN001
        """Fail on the third shard, after two have been published."""
        calls["count"] += 1
        if calls["count"] == 3:
            raise ChunkIOError("simulated disk failure")
        return real_write(chunk, path, **kwargs)

    monkeypatch.setattr(producer, "write_chunk_atomic", flaky)

    with pytest.raises(ChunkIOError, match="simulated disk failure"):
        produce_run(scenario_config(total_points=1000, chunk_points=256), tmp_path)

    run_dir = single_run_dir(tmp_path)
    assert not (run_dir / "manifest.json").exists()
    assert len(list((run_dir / "raw").glob("part-*.npz"))) == 2
    assert not (run_dir / "private_truth").exists()


def test_failed_truth_write_publishes_no_complete_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    """Issue #45: a failed truth write must not publish a complete manifest.

    The truth is written before the manifest, so a truth write failure leaves
    the shards on disk but publishes no manifest.  A consumer that follows
    ``docs/interface.md`` section 5.2 rejects such a run because the manifest
    is absent -- it must never see a ``complete`` manifest whose truth file
    does not exist.
    """
    module = types.ModuleType("g15.simulation.faults")

    class FakeEvent:
        """A single injected event, as ``inject_faults`` would return it."""

        event_id = "F001"
        device_id = "DEV-001"
        type = "spike"
        start_seq = 10
        end_seq_exclusive = 20
        amplitude = 5.0

        def to_dict(self) -> dict[str, object]:
            """Return the frozen truth representation."""
            return {
                "event_id": self.event_id,
                "device_id": self.device_id,
                "type": self.type,
                "start_seq": self.start_seq,
                "end_seq_exclusive": self.end_seq_exclusive,
                "amplitude": self.amplitude,
            }

    def fake_inject(chunk, config, rng=None):  # noqa: ANN001, ARG001
        """Return the chunk untouched plus one event."""
        return chunk, [FakeEvent()]

    module.inject_faults = fake_inject  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "g15.simulation.faults", module)

    def failing_truth(run_dir, run_id, truth):  # noqa: ANN001
        """Fail the way a full disk would."""
        raise OSError(28, "No space left on device (simulated)")

    monkeypatch.setattr(producer, "_write_truth", failing_truth)

    with pytest.raises(OSError, match="No space left on device"):
        produce_run(scenario_config(total_points=1000, chunk_points=256), tmp_path)

    run_dir = single_run_dir(tmp_path)
    manifest = run_dir / "manifest.json"
    assert not manifest.exists(), "a complete manifest must not survive a truth failure"
    assert not (run_dir / "private_truth").exists()
    # The shards stay on disk, exactly like a failed shard write; the absent
    # manifest is what makes the run unconsumable.
    assert len(list((run_dir / "raw").glob("part-*.npz"))) == 4


def test_missing_chunk_points_is_rejected(tmp_path: Path) -> None:
    """``scenario.chunk_points`` has no safe default."""
    config = scenario_config()
    del config["scenario"]["chunk_points"]  # type: ignore[index]

    with pytest.raises(ValueError, match="chunk_points"):
        produce_run(config, tmp_path)


@pytest.mark.parametrize("chunk_points", [0, -5, 2.5, True])
def test_invalid_chunk_points_is_rejected(tmp_path: Path, chunk_points: object) -> None:
    """Zero, negative, fractional and boolean shard sizes are all invalid."""
    config = scenario_config()
    config["scenario"]["chunk_points"] = chunk_points  # type: ignore[index]

    with pytest.raises(ValueError, match="chunk_points"):
        produce_run(config, tmp_path)


# --------------------------------------------------------------------------- #
# int64 timestamp range (R-1)
# --------------------------------------------------------------------------- #

#: A realistic start instant, ``2026-10-14T00:00:00Z`` in UTC nanoseconds.
START_NS = 1_791_936_000_000_000_000


def test_a_run_past_the_int64_horizon_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that cannot be timestamped is refused before any output exists.

    Regression for R-1: ``simulate_chunk`` builds ``timestamp_ns`` in ``int64``
    and wraps silently, so a long enough run published negative stamps that
    passed every shape and finiteness check.  ``simulate_chunk`` is stubbed out
    so a missing guard fails fast instead of generating 10**12 samples.
    """

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("guard did not fire: simulate_chunk was called")

    monkeypatch.setattr(producer, "simulate_chunk", _forbidden)
    output_root = tmp_path / "runs"

    with pytest.raises(ValueError, match="overflows int64"):
        produce_run_detailed(
            scenario_config(total_points=10**12, chunk_points=1024), output_root
        )

    assert not output_root.exists(), "a refused run must not create anything"


def test_timestamp_guard_boundary_is_exact() -> None:
    """The guard must reject the first unrepresentable point, not more.

    A conservative bound would also refuse a boundary-legal configuration, so
    the check is verified from both sides: ``limit`` fits, ``limit + 1`` does
    not.  Verified for the shipped rate and for a rate that does not divide the
    nanosecond.
    """
    for rate in (100, 7):
        limit = producer._max_total_points(START_NS, rate)

        assert producer._timestamp_offset_fits(
            limit - 1, START_NS, rate
        ), f"last sample of a {limit}-point run must fit at rate {rate}"
        assert not producer._timestamp_offset_fits(
            limit, START_NS, rate
        ), f"sample {limit} must not fit at rate {rate}"


def test_shipped_benchmark_is_far_below_the_limit() -> None:
    """The 1,000,000-point acceptance run must never trip the guard."""
    assert producer._max_total_points(START_NS, 100) > 1_000_000 * 1000


def test_start_instant_below_the_int64_floor_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start instant before 1677-09-21 is refused before any output exists.

    This is the *other* end of the same ``int64`` range.  ``start_time_utc``
    alone can overflow on sample 0, in which case no ``total_points`` value
    helps; the guard must catch it as a configuration error rather than let
    NumPy raise ``OverflowError`` midway through the run, after the output
    directory already exists.
    """

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("guard did not fire: simulate_chunk was called")

    monkeypatch.setattr(producer, "simulate_chunk", _forbidden)
    output_root = tmp_path / "runs"

    with pytest.raises(ValueError, match="start_time_utc is outside"):
        produce_run_detailed(
            scenario_config(total_points=8, start_time_utc="1600-01-01T00:00:00Z"),
            output_root,
        )

    assert not output_root.exists(), "a refused run must not create anything"


def test_start_instant_at_the_int64_floor_is_accepted(tmp_path: Path) -> None:
    """The floor itself is representable, so it must not be refused.

    ``1677-09-21T00:12:44Z`` is the first whole second that fits; refusing it
    would be an off-by-one in the guard rather than a contract violation.
    """
    run_dir = produce_run(
        scenario_config(total_points=8, start_time_utc="1677-09-21T00:12:44Z"),
        tmp_path,
    )

    assert (run_dir / "manifest.json").is_file()


# --------------------------------------------------------------------------- #
# Summary and run log
# --------------------------------------------------------------------------- #


def test_summary_reports_the_measurement_window(tmp_path: Path) -> None:
    """The summary must expose the acceptance-section-2 numbers."""
    summary = produce_run_detailed(
        scenario_config(total_points=1000, chunk_points=256), tmp_path
    )

    assert isinstance(summary, ProductionSummary)
    assert summary.elapsed_seconds > 0
    assert summary.total_points == 1000
    assert summary.scalar_value_count == 2000, "two signals per sample"
    assert summary.part_count == 4
    assert summary.run_dir == single_run_dir(tmp_path)


def test_run_log_records_the_completion(tmp_path: Path) -> None:
    """``logs/app.jsonl`` is the evidence trail for a published run."""
    summary = produce_run_detailed(scenario_config(total_points=512), tmp_path)

    lines = (
        (summary.run_dir / "logs" / "app.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["component"] == "producer"
    assert record["event"] == "run_complete"
    assert record["run_id"] == summary.run_dir.name
    assert record["part_count"] == 2
    assert record["total_points"] == 512
    assert record["elapsed_ms"] >= 0
