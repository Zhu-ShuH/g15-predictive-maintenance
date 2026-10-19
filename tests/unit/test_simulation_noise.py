"""Unit tests for the G15 seeded noise module (Issue #5, owner B1).

The critical property under test is *chunk-addressable* randomness: a sample's
value is a function of ``(seed, stream, absolute index)``, so shard size, shard
order and retries cannot change the generated data.
"""

from __future__ import annotations

import numpy as np
import pytest

from g15.simulation.noise import (
    BLOCK_POINTS,
    STREAM_TEMPERATURE,
    STREAM_VIBRATION,
    gaussian_noise,
    standard_normal,
    stream_entropy,
)


def test_same_arguments_are_reproducible() -> None:
    """Identical arguments must give bit-identical output."""
    first = standard_normal(42, STREAM_VIBRATION, 1000, 4096)
    second = standard_normal(42, STREAM_VIBRATION, 1000, 4096)

    assert np.array_equal(first, second)


def test_different_seed_gives_different_stream() -> None:
    """The seed must actually reach the generator."""
    assert not np.array_equal(
        standard_normal(42, STREAM_VIBRATION, 0, 1024),
        standard_normal(43, STREAM_VIBRATION, 0, 1024),
    )


def test_different_stream_names_are_independent() -> None:
    """Each channel owns its own sub-stream, so one change cannot move the other."""
    vibration = standard_normal(42, STREAM_VIBRATION, 0, 4096)
    temperature = standard_normal(42, STREAM_TEMPERATURE, 0, 4096)

    assert not np.array_equal(vibration, temperature)
    correlation = float(np.corrcoef(vibration, temperature)[0, 1])
    assert abs(correlation) < 0.05


def test_random_access_equals_sequential_access() -> None:
    """Any sub-range must equal the same slice of the full range."""
    full = standard_normal(7, STREAM_VIBRATION, 0, 100_000)

    for start, count in ((0, 10), (99, 3), (16_383, 2), (16_384, 5), (99_000, 1_000)):
        assert np.array_equal(
            standard_normal(7, STREAM_VIBRATION, start, count),
            full[start : start + count],
        ), f"mismatch for [{start}, {start + count})"


def test_chunking_does_not_change_values() -> None:
    """Regenerating in pieces must reproduce the one-shot range exactly."""
    one_shot = standard_normal(11, STREAM_TEMPERATURE, 0, 5 * BLOCK_POINTS)
    pieces = np.concatenate(
        [
            standard_normal(11, STREAM_TEMPERATURE, i * BLOCK_POINTS, BLOCK_POINTS)
            for i in range(5)
        ]
    )

    assert np.array_equal(one_shot, pieces)


def test_dtype_and_shape_are_float64() -> None:
    """The noise must arrive as ``float64`` with the requested length."""
    values = standard_normal(1, STREAM_VIBRATION, 0, 100)

    assert values.dtype == np.float64
    assert values.shape == (100,)


def test_zero_count_returns_empty_array() -> None:
    """An empty request must not touch the generator."""
    values = standard_normal(1, STREAM_VIBRATION, 500, 0)

    assert values.shape == (0,)
    assert values.dtype == np.float64


def test_negative_arguments_are_rejected() -> None:
    """Negative indices and counts are invalid input."""
    with pytest.raises(ValueError, match="start must be non-negative"):
        standard_normal(1, STREAM_VIBRATION, -1, 10)
    with pytest.raises(ValueError, match="count must be non-negative"):
        standard_normal(1, STREAM_VIBRATION, 0, -10)
    with pytest.raises(ValueError, match="seed must be non-negative"):
        standard_normal(-1, STREAM_VIBRATION, 0, 10)


def test_non_integral_arguments_are_rejected() -> None:
    """Float indices that are not whole numbers must fail, not silently truncate."""
    with pytest.raises(ValueError, match="whole number"):
        standard_normal(1, STREAM_VIBRATION, 1.5, 10)
    with pytest.raises(ValueError, match="whole number"):
        standard_normal(1, STREAM_VIBRATION, 0, 10.5)


def test_invalid_stream_name_is_rejected() -> None:
    """The stream name is part of the addressing scheme and must be a string."""
    with pytest.raises(ValueError, match="stream name must be a non-empty string"):
        standard_normal(1, "", 0, 4)
    with pytest.raises(ValueError, match="stream name must be a non-empty string"):
        standard_normal(1, None, 0, 4)  # type: ignore[arg-type]


def test_stream_entropy_is_stable_and_well_formed() -> None:
    """Entropy must be plain non-negative integers and platform independent."""
    entropy = stream_entropy(42, STREAM_VIBRATION, 3)

    assert entropy == stream_entropy(42, STREAM_VIBRATION, 3)
    assert all(isinstance(part, int) and part >= 0 for part in entropy)
    assert len(entropy) == 3
    # Different blocks must land on different entropy.
    assert stream_entropy(42, STREAM_VIBRATION, 4) != entropy
    # And so must different streams at the same block.
    assert stream_entropy(42, STREAM_TEMPERATURE, 3) != entropy


def test_block_size_is_validated() -> None:
    """A non-positive block size has no meaningful grid."""
    with pytest.raises(ValueError, match="block_points must be positive"):
        standard_normal(1, STREAM_VIBRATION, 0, 4, block_points=0)
    with pytest.raises(ValueError, match="block_points must be positive"):
        standard_normal(1, STREAM_VIBRATION, 0, 4, block_points=-8)


def test_custom_block_size_still_matches_sequential_access() -> None:
    """The grid size is an implementation detail, not an observable."""
    full = standard_normal(3, STREAM_VIBRATION, 0, 20_000, block_points=64)
    tail = standard_normal(3, STREAM_VIBRATION, 12_345, 100, block_points=64)

    assert np.array_equal(tail, full[12_345:12_445])


def test_gaussian_noise_scales_standard_normal() -> None:
    """``gaussian_noise`` must be exactly ``std`` times the unit stream."""
    unit = standard_normal(5, STREAM_TEMPERATURE, 0, 500)
    scaled = gaussian_noise(5, STREAM_TEMPERATURE, 0, 500, 0.15)

    assert np.allclose(scaled, unit * 0.15, rtol=0.0, atol=0.0)


def test_gaussian_noise_has_expected_moments() -> None:
    """The empirical mean and deviation must match ``N(0, std**2)``."""
    values = gaussian_noise(9, STREAM_VIBRATION, 0, 200_000, 0.05)

    assert abs(float(values.mean())) < 0.001
    assert abs(float(values.std(ddof=1)) - 0.05) < 0.001


def test_zero_std_returns_exact_zeros() -> None:
    """Zero noise must short-circuit to exact zeros, not near-zeros."""
    values = gaussian_noise(9, STREAM_VIBRATION, 0, 1000, 0.0)

    assert np.array_equal(values, np.zeros(1000))
    assert values.dtype == np.float64


def test_gaussian_noise_rejects_invalid_std() -> None:
    """A negative or non-finite deviation is a configuration error."""
    with pytest.raises(ValueError, match="noise_std must be non-negative"):
        gaussian_noise(9, STREAM_VIBRATION, 0, 16, -0.5)
    with pytest.raises(ValueError, match="must be finite"):
        gaussian_noise(9, STREAM_VIBRATION, 0, 16, float("nan"))
    with pytest.raises(ValueError, match="must be finite"):
        gaussian_noise(9, STREAM_VIBRATION, 0, 16, float("inf"))


def test_large_seed_values_are_supported() -> None:
    """Seeds outside the 32-bit range must still address a stream."""
    values = standard_normal(2**40 + 7, STREAM_VIBRATION, 0, 64)

    assert values.shape == (64,)
    assert bool(np.all(np.isfinite(values)))


def test_golden_values_pin_the_numeric_contract() -> None:
    """Hard-coded samples guard the numeric contract, not just self-consistency.

    Every other test in this module compares the implementation with itself, so
    a silent change to ``BLOCK_POINTS`` or to the entropy formula would keep all
    of them green while changing every number in every generated dataset. These
    literals were produced by the current implementation and are pinned on
    purpose: moving any of them is a reviewed contract change, not a refactor
    side effect (``docs/interface.md`` section 6 treats the block grid as a
    numeric contract, not an implementation detail).

    The second call starts two points before a block boundary so a change to
    the grid shows up as a discontinuity instead of a uniform shift.
    """
    assert list(stream_entropy(42, STREAM_VIBRATION, 0)) == [42, 3_216_066_249, 0]

    head = standard_normal(42, STREAM_VIBRATION, 0, 4)
    assert head.tolist() == [
        -0.6869564307698991,
        -0.8351377905724985,
        -0.08340827791750291,
        0.9252695431570744,
    ]

    crossing = standard_normal(42, STREAM_VIBRATION, BLOCK_POINTS - 2, 4)
    assert crossing.tolist() == [
        0.17703369886443934,
        -1.1352805402178385,
        -1.2608326789513238,
        0.1603717396662623,
    ]
