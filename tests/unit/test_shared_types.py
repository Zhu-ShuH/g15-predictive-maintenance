"""Public chunk identity, validation and migration compatibility (Issue #17)."""

from __future__ import annotations

import copy
from dataclasses import replace

import numpy as np
import pytest

from g15.common.types import SimulationChunk
from g15.simulation import engine


def make_chunk(count: int = 3) -> SimulationChunk:
    """Return explicit, finite arrays including valid negative measurements."""
    return SimulationChunk(
        seq=np.arange(count, dtype=np.int64),
        timestamp_ns=np.arange(count, dtype=np.int64) * 10_000_000,
        vibration=np.full(count, -0.5, dtype=np.float64),
        temperature=np.full(count, -2.0, dtype=np.float64),
    )


def test_legacy_import_is_the_unique_public_class() -> None:
    """Existing import sites and isinstance checks retain one type identity."""
    assert engine.SimulationChunk is SimulationChunk
    assert "SimulationChunk" in engine.__all__
    assert SimulationChunk.__module__ == "g15.common.types"
    chunk = make_chunk()
    assert isinstance(chunk, engine.SimulationChunk)
    assert chunk.validate() is None
    assert chunk.sample_count == 3
    assert chunk.scalar_value_count == 6
    assert (chunk.first_seq, chunk.last_seq_exclusive) == (0, 3)
    np.testing.assert_array_equal(chunk.vibration, [-0.5, -0.5, -0.5])


def test_empty_memory_chunk_keeps_public_properties() -> None:
    """Empty arrays are valid in memory and preserve their exact dtypes."""
    chunk = make_chunk(0)
    assert chunk.validate() is None
    assert len(chunk) == chunk.sample_count == chunk.scalar_value_count == 0
    assert (chunk.first_seq, chunk.last_seq_exclusive) == (-1, -1)
    assert [array.dtype for array in (chunk.seq, chunk.timestamp_ns)] == [
        np.dtype("int64"),
        np.dtype("int64"),
    ]
    assert chunk.vibration.dtype == chunk.temperature.dtype == np.dtype("float64")


@pytest.mark.parametrize("field", ["seq", "timestamp_ns", "vibration", "temperature"])
def test_each_array_rejects_two_dimensions(field: str) -> None:
    """A shape error must fail even when all element counts match."""
    chunk = make_chunk()
    bad = replace(chunk, **{field: getattr(chunk, field).reshape(1, 3)})
    with pytest.raises(ValueError, match=f"{field} must be one-dimensional"):
        bad.validate()


@pytest.mark.parametrize(
    ("field", "wrong_dtype"),
    [
        ("seq", "int32"),
        ("timestamp_ns", "float64"),
        ("vibration", "float32"),
        ("temperature", "int64"),
    ],
)
def test_each_array_rejects_wrong_dtype(field: str, wrong_dtype: str) -> None:
    """The migration must not weaken the persisted array dtype contract."""
    chunk = make_chunk()
    bad = replace(chunk, **{field: getattr(chunk, field).astype(wrong_dtype)})
    with pytest.raises(ValueError, match=f"{field} must be"):
        bad.validate()


def test_length_mismatch_and_seq_gap_are_rejected() -> None:
    """Length and continuity validation remain separate, falsifiable checks."""
    chunk = make_chunk()
    with pytest.raises(ValueError, match="equal length"):
        replace(chunk, temperature=np.zeros(2, dtype=np.float64)).validate()
    with pytest.raises(ValueError, match="contiguous"):
        replace(chunk, seq=np.array([0, 1, 3], dtype=np.int64)).validate()


@pytest.mark.parametrize("field", ["vibration", "temperature"])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_signals_remain_rejected(field: str, invalid: float) -> None:
    """Both channels must reject non-finite values at the reported index."""
    chunk = make_chunk()
    values = getattr(chunk, field).copy()
    values[1] = invalid
    with pytest.raises(ValueError, match=f"{field} is not finite at local index 1"):
        replace(chunk, **{field: values}).validate()


def test_engine_constructs_shared_type_without_consuming_rng() -> None:
    """Existing callers obtain the public class and retain caller RNG state."""
    config = {
        "seed": 42,
        "sample_rate_hz": 100,
        "total_points": 20_000,
        "device_id": "DEV-001",
        "start_time_utc": "2026-10-14T00:00:00Z",
        "vibration": {
            "baseline": 0.5,
            "degradation_rate_per_sample": 0.0,
            "noise_std": 0.05,
            "unit": "g",
        },
        "temperature": {
            "baseline": 35.0,
            "degradation_rate_per_sample": 0.0,
            "noise_std": 0.15,
            "unit": "degC",
        },
    }
    generator = np.random.default_rng(7)
    before = copy.deepcopy(generator.bit_generator.state)
    chunk = engine.simulate_chunk(config, 16_380, 8, generator)
    assert type(chunk) is SimulationChunk
    assert before == generator.bit_generator.state
    np.testing.assert_array_equal(chunk.seq, np.arange(16_380, 16_388, dtype=np.int64))
    assert chunk.validate() is None
    with pytest.raises(TypeError, match="rng"):
        engine.simulate_chunk(config, 0, 1, object())
