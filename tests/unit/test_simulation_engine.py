"""Unit tests for the G15 simulation kernel (Issue #5, owner B1).

Covers the acceptance criteria of ``members/B1/AGENT.md``: same seed identical,
different seed different, trend direction, dtype/shape/units, empty and negative
point counts, invalid noise, incomplete last block, and both signal channels.

The scenario fixtures here are plain mappings so that the tests do not depend on
``g15.config`` (Issue #4, owned by C3, is still open).  ``simulate_chunk`` reads
the configuration structurally, so both layouts are supported.
"""

from __future__ import annotations

import copy
import datetime as dt
import math

import numpy as np
import pytest

from g15.simulation.degradation import SignalParameters, linear_trend
from g15.simulation.engine import (
    MAX_SAMPLE_RATE_HZ,
    NANOSECONDS_PER_SECOND,
    SimulationChunk,
    load_scenario_parameters,
    simulate_chunk,
    timestamp_ns_for,
)
from g15.simulation.noise import STREAM_TEMPERATURE, STREAM_VIBRATION, gaussian_noise

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

DEFAULT_SCENARIO = {
    "schema_version": "1.0",
    "scenario": {
        "id": "bearing_001",
        "seed": 42,
        "start_time_utc": "2026-10-14T00:00:00Z",
        "device_id": "DEV-001",
        "sample_rate_hz": 100,
        "total_points": 1_000_000,
        "chunk_points": 102_400,
    },
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


def scenario(**overrides: object) -> dict:
    """Return a deep copy of the default scenario with ``scenario`` overrides."""
    config = {
        "scenario": dict(DEFAULT_SCENARIO["scenario"]),
        "signals": {
            "vibration": dict(DEFAULT_SCENARIO["signals"]["vibration"]),
            "temperature": dict(DEFAULT_SCENARIO["signals"]["temperature"]),
        },
    }
    config["scenario"].update(overrides)
    return config


def signals(**overrides: object) -> dict:
    """Return the default scenario with ``signals`` overrides applied."""
    config = scenario()
    for channel, values in overrides.items():
        config["signals"][channel].update(values)  # type: ignore[arg-type]
    return config


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #


def test_same_seed_is_bit_identical() -> None:
    """Same configuration and seed must produce point-for-point identical data."""
    first = simulate_chunk(DEFAULT_SCENARIO, 0, 4096)
    second = simulate_chunk(DEFAULT_SCENARIO, 0, 4096)

    assert np.array_equal(first.seq, second.seq)
    assert np.array_equal(first.timestamp_ns, second.timestamp_ns)
    assert np.array_equal(first.vibration, second.vibration)
    assert np.array_equal(first.temperature, second.temperature)


def test_different_seed_changes_noise_but_not_trend() -> None:
    """A different seed must change the noise, and only the noise."""
    other = scenario(seed=43)
    base = simulate_chunk(DEFAULT_SCENARIO, 0, 4096)
    shifted = simulate_chunk(other, 0, 4096)

    assert not np.array_equal(base.vibration, shifted.vibration)
    assert not np.array_equal(base.temperature, shifted.temperature)

    # The deterministic trend is seed-independent, so the difference must be
    # zero-mean noise rather than a shifted baseline.
    delta = base.vibration - shifted.vibration
    assert abs(float(delta.mean())) < 0.01
    assert float(delta.std()) > 0.0

    # Timestamps and indices never depend on the seed.
    assert np.array_equal(base.seq, shifted.seq)
    assert np.array_equal(base.timestamp_ns, shifted.timestamp_ns)


def test_chunking_does_not_change_values() -> None:
    """Splitting a range must not move a single sample (retry/resume safety)."""
    whole = simulate_chunk(DEFAULT_SCENARIO, 0, 4096)
    parts = [
        simulate_chunk(DEFAULT_SCENARIO, start, 1024) for start in (0, 1024, 2048, 3072)
    ]

    assert np.array_equal(whole.vibration, np.concatenate([p.vibration for p in parts]))
    assert np.array_equal(
        whole.temperature, np.concatenate([p.temperature for p in parts])
    )
    assert np.array_equal(
        whole.timestamp_ns, np.concatenate([p.timestamp_ns for p in parts])
    )
    assert np.array_equal(whole.seq, np.concatenate([p.seq for p in parts]))


def test_random_access_matches_sequential_access() -> None:
    """An out-of-order chunk must equal the same slice of a one-shot run."""
    whole = simulate_chunk(DEFAULT_SCENARIO, 0, 4096)
    tail = simulate_chunk(DEFAULT_SCENARIO, 3000, 1096)

    assert np.array_equal(tail.vibration, whole.vibration[3000:])
    assert np.array_equal(tail.temperature, whole.temperature[3000:])
    assert np.array_equal(tail.timestamp_ns, whole.timestamp_ns[3000:])


def test_rng_argument_is_accepted_and_type_checked() -> None:
    """``rng`` keeps the frozen signature but must not influence the result."""
    generator = np.random.default_rng(7)
    before = copy.deepcopy(generator.bit_generator.state)
    with_rng = simulate_chunk(DEFAULT_SCENARIO, 0, 512, generator)
    after = copy.deepcopy(generator.bit_generator.state)
    without = simulate_chunk(DEFAULT_SCENARIO, 0, 512)

    assert np.array_equal(with_rng.vibration, without.vibration)
    # The generator must be left exactly as handed over: not advanced, not reseeded.
    # Comparing the whole state (not just one counter) fails if any draw happened.
    assert after == before

    with pytest.raises(TypeError, match="numpy.random.Generator"):
        simulate_chunk(DEFAULT_SCENARIO, 0, 16, 12345)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Shapes, dtypes and units
# --------------------------------------------------------------------------- #


def test_dtypes_and_shape_match_frozen_contract() -> None:
    """The four arrays must be int64/int64/float64/float64 and equally long."""
    chunk = simulate_chunk(DEFAULT_SCENARIO, 0, 1000)

    assert chunk.seq.dtype == np.int64
    assert chunk.timestamp_ns.dtype == np.int64
    assert chunk.vibration.dtype == np.float64
    assert chunk.temperature.dtype == np.float64
    assert chunk.seq.shape == (1000,)
    assert chunk.timestamp_ns.shape == (1000,)
    assert chunk.vibration.shape == (1000,)
    assert chunk.temperature.shape == (1000,)
    assert len(chunk) == 1000


def test_seq_starts_at_start_seq_and_is_contiguous() -> None:
    """``seq`` must continue the run, not restart at zero for each chunk."""
    chunk = simulate_chunk(DEFAULT_SCENARIO, 5120, 256)

    assert int(chunk.seq[0]) == 5120
    assert int(chunk.seq[-1]) == 5375
    assert np.array_equal(np.diff(chunk.seq), np.ones(255, dtype=np.int64))
    assert chunk.first_seq == 5120
    assert chunk.last_seq_exclusive == 5376


def test_sample_count_and_scalar_value_count_are_distinct() -> None:
    """One sample carries two scalars; the two counts must not be conflated."""
    chunk = simulate_chunk(DEFAULT_SCENARIO, 0, 500)

    assert chunk.sample_count == 500
    assert chunk.scalar_value_count == 1000


def test_units_are_carried_from_configuration() -> None:
    """Channel units are reported, not used in arithmetic."""
    params = load_scenario_parameters(DEFAULT_SCENARIO)

    assert params.vibration.unit == "g"
    assert params.temperature.unit == "degC"
    assert params.sample_rate_hz == 100
    assert params.total_points == 1_000_000


# --------------------------------------------------------------------------- #
# Trend direction
# --------------------------------------------------------------------------- #


def test_degradation_trend_rises_for_both_channels() -> None:
    """Positive degradation rates must produce an upward drift in both signals."""
    chunk = simulate_chunk(DEFAULT_SCENARIO, 0, 100_000)

    # Compare window means so that noise does not dominate the assertion.
    vibration = chunk.vibration.reshape(10, -1).mean(axis=1)
    temperature = chunk.temperature.reshape(10, -1).mean(axis=1)

    assert bool(np.all(np.diff(vibration) > 0))
    assert bool(np.all(np.diff(temperature) > 0))


def test_zero_rate_is_flat_apart_from_noise() -> None:
    """A zero degradation rate must leave the baseline flat in expectation."""
    config = signals(
        vibration={"degradation_rate_per_sample": 0.0},
        temperature={"degradation_rate_per_sample": 0.0},
    )
    chunk = simulate_chunk(config, 0, 100_000)

    assert abs(float(chunk.vibration.mean()) - 0.5) < 0.01
    assert abs(float(chunk.temperature.mean()) - 35.0) < 0.01
    # And the first half must not drift away from the second half.
    first, second = chunk.vibration[:50_000], chunk.vibration[50_000:]
    assert abs(float(first.mean()) - float(second.mean())) < 0.01


def test_negative_rate_is_rejected() -> None:
    """Self-repair is not modelled; a negative rate is a configuration error."""
    config = signals(vibration={"degradation_rate_per_sample": -0.001})

    with pytest.raises(ValueError, match="must be non-negative"):
        simulate_chunk(config, 0, 16)


def test_trend_matches_the_closed_form_formula() -> None:
    """With zero noise the samples must equal ``v0 + kv * i`` exactly."""
    config = signals(
        vibration={"noise_std": 0.0},
        temperature={"noise_std": 0.0},
    )
    start, count = 1234, 100
    chunk = simulate_chunk(config, start, count)

    expected = 0.5 + 0.000001 * np.arange(start, start + count, dtype=np.float64)
    assert np.allclose(chunk.vibration, expected, rtol=0.0, atol=0.0)
    expected_temp = 35.0 + 0.000003 * np.arange(start, start + count, dtype=np.float64)
    assert np.allclose(chunk.temperature, expected_temp, rtol=0.0, atol=0.0)


def test_noise_standard_deviation_is_respected() -> None:
    """The empirical noise scale must match the configured standard deviation."""
    chunk = simulate_chunk(DEFAULT_SCENARIO, 0, 200_000)
    trend = 0.5 + 0.000001 * np.arange(200_000, dtype=np.float64)
    residual = chunk.vibration - trend

    assert abs(float(residual.mean())) < 0.001
    assert math.isclose(float(residual.std(ddof=1)), 0.05, rel_tol=0.05)


# --------------------------------------------------------------------------- #
# Boundary and error handling
# --------------------------------------------------------------------------- #


def test_zero_count_returns_empty_chunk_with_correct_dtypes() -> None:
    """A degenerate last shard must be representable without special-casing."""
    chunk = simulate_chunk(DEFAULT_SCENARIO, 1_000_000, 0)

    assert isinstance(chunk, SimulationChunk)
    assert len(chunk) == 0
    assert chunk.seq.dtype == np.int64
    assert chunk.timestamp_ns.dtype == np.int64
    assert chunk.vibration.dtype == np.float64
    assert chunk.temperature.dtype == np.float64
    assert chunk.scalar_value_count == 0
    assert chunk.first_seq == -1
    assert chunk.last_seq_exclusive == -1
    chunk.validate()


def test_negative_count_is_rejected() -> None:
    """A negative point count is invalid input, not an empty chunk."""
    with pytest.raises(ValueError, match="count must be non-negative"):
        simulate_chunk(DEFAULT_SCENARIO, 0, -1)


def test_negative_start_seq_is_rejected() -> None:
    """``seq`` starts at zero by contract; negative indices are invalid."""
    with pytest.raises(ValueError, match="start_seq must be non-negative"):
        simulate_chunk(DEFAULT_SCENARIO, -1, 16)


def test_chunk_beyond_total_points_is_rejected() -> None:
    """A chunk may not run past the declared length of the run."""
    with pytest.raises(ValueError, match="exceeds scenario.total_points"):
        simulate_chunk(DEFAULT_SCENARIO, 999_990, 100)


def test_last_incomplete_block_is_generated_in_full() -> None:
    """The final shard may be shorter than ``chunk_points`` and still be valid."""
    chunk_points = int(DEFAULT_SCENARIO["scenario"]["chunk_points"])
    total = int(DEFAULT_SCENARIO["scenario"]["total_points"])
    remainder = total % chunk_points

    assert remainder != 0  # the fixture is meant to exercise the tail
    chunk = simulate_chunk(DEFAULT_SCENARIO, total - remainder, remainder)

    assert len(chunk) == remainder
    assert int(chunk.seq[-1]) == total - 1
    chunk.validate()


def test_invalid_noise_is_rejected() -> None:
    """Negative or non-finite noise parameters must fail loudly."""
    with pytest.raises(ValueError, match="noise_std must be non-negative"):
        simulate_chunk(signals(vibration={"noise_std": -0.01}), 0, 16)

    with pytest.raises(ValueError, match="must be finite"):
        simulate_chunk(signals(vibration={"noise_std": float("nan")}), 0, 16)

    with pytest.raises(ValueError, match="must be finite"):
        simulate_chunk(signals(temperature={"noise_std": float("inf")}), 0, 16)


def test_non_finite_baseline_is_rejected() -> None:
    """NaN or infinite baselines must be caught before any arithmetic."""
    with pytest.raises(ValueError, match="must be finite"):
        simulate_chunk(signals(vibration={"baseline": float("nan")}), 0, 16)


def test_overflowing_trend_raises_instead_of_silently_returning_inf() -> None:
    """An absurd degradation rate must raise, naming the offending channel."""
    config = signals(vibration={"degradation_rate_per_sample": 1e308})

    with pytest.raises(ValueError, match="vibration is not finite"):
        simulate_chunk(config, 0, 8)


def test_missing_fields_are_reported_with_their_path() -> None:
    """A malformed configuration must name the missing field, not crash obscurely."""
    broken = scenario()
    del broken["scenario"]["sample_rate_hz"]

    with pytest.raises(ValueError, match=r"scenario\.sample_rate_hz"):
        simulate_chunk(broken, 0, 16)


def test_non_positive_sample_rate_is_rejected() -> None:
    """A zero or negative sampling rate has no meaningful clock."""
    with pytest.raises(ValueError, match="sample_rate_hz must be positive"):
        simulate_chunk(scenario(sample_rate_hz=0), 0, 16)


def test_non_positive_total_points_is_rejected() -> None:
    """A run must declare at least one sample."""
    with pytest.raises(ValueError, match="total_points must be positive"):
        simulate_chunk(scenario(total_points=0), 0, 16)


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #


def test_timestamps_are_strictly_increasing_at_the_sample_rate() -> None:
    """The clock must advance by exactly one sampling period per sample."""
    chunk = simulate_chunk(DEFAULT_SCENARIO, 0, 1000)
    steps = np.diff(chunk.timestamp_ns)

    assert bool(np.all(steps == NANOSECONDS_PER_SECOND // 100))


def test_timestamps_are_exact_at_low_sample_rates() -> None:
    """Integer rounding must not drift or produce duplicate timestamps."""
    config = scenario(sample_rate_hz=3)
    chunk = simulate_chunk(config, 0, 9)

    assert list(chunk.timestamp_ns - chunk.timestamp_ns[0]) == [
        0,
        333_333_333,
        666_666_667,
        1_000_000_000,
        1_333_333_333,
        1_666_666_667,
        2_000_000_000,
        2_333_333_333,
        2_666_666_667,
    ]
    assert bool(np.all(np.diff(chunk.timestamp_ns) > 0))


def test_timestamp_origin_is_the_configured_start_time() -> None:
    """``timestamp_ns[0]`` must be the configured UTC start instant."""
    chunk = simulate_chunk(DEFAULT_SCENARIO, 0, 4)
    expected = (
        int(dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc).timestamp())
        * NANOSECONDS_PER_SECOND
    )

    assert int(chunk.timestamp_ns[0]) == expected


def test_timestamp_ns_for_is_pure_and_vectorised() -> None:
    """The clock helper must be usable on its own, with the same result."""
    base = 1_700_000_000 * NANOSECONDS_PER_SECOND
    direct = timestamp_ns_for(0, 200, base, 100)
    chunk = simulate_chunk(DEFAULT_SCENARIO, 0, 200)

    assert direct.dtype == np.int64
    assert list(direct[:3]) == [
        base,
        base + 10_000_000,
        base + 20_000_000,
    ]
    # Same shape contract, independent of the scenario's own start time.
    assert direct.shape == chunk.timestamp_ns.shape


# --------------------------------------------------------------------------- #
# int64 clock guard (hand-computed boundaries)
# --------------------------------------------------------------------------- #

_INT64_MAX = 2**63 - 1
_INT64_MIN = -(2**63)


def test_clock_guard_accepts_the_exact_int64_upper_bound() -> None:
    """The last representable nanosecond must still be produced, not refused.

    ``sample_rate_hz=7`` does not divide 1e9, so sample 1 sits
    ``round_half_up(1e9 / 7) = 142_857_143`` ns after the start instant.  With
    ``start_time_utc_ns = _INT64_MAX - 142_857_143`` the clock lands exactly on
    ``_INT64_MAX``, which is representable and must be accepted.
    """
    offset = (1 * NANOSECONDS_PER_SECOND + 7 // 2) // 7
    assert offset == 142_857_143

    stamps = timestamp_ns_for(1, 1, _INT64_MAX - offset, 7)

    assert list(stamps) == [_INT64_MAX]


def test_clock_guard_uses_half_up_rounding_not_floor() -> None:
    """A floor-rounded bound would let this one-sample chunk wrap silently.

    ``floor(1e9 / 6) = 166_666_666`` but the kernel rounds half up to
    ``166_666_667``.  Starting at ``_INT64_MAX - 166_666_666`` therefore ends
    one nanosecond past ``_INT64_MAX``; the guard must refuse it rather than
    return ``_INT64_MIN``.
    """
    floor_offset = NANOSECONDS_PER_SECOND // 6
    half_up_offset = (NANOSECONDS_PER_SECOND + 6 // 2) // 6
    assert floor_offset == 166_666_666
    assert half_up_offset == 166_666_667

    with pytest.raises(ValueError, match="do not fit in int64"):
        timestamp_ns_for(1, 1, _INT64_MAX - floor_offset, 6)


def test_clock_guard_rejects_a_start_instant_below_int64() -> None:
    """An out-of-range start must raise ValueError, not OverflowError.

    NumPy raises ``OverflowError: int too big to convert`` when the scalar start
    instant does not fit its ``int64`` cast, and that error is outside the
    documented ``ValueError`` family.
    """
    with pytest.raises(ValueError, match="outside the int64"):
        timestamp_ns_for(0, 2, _INT64_MIN - 1, 1)

    # The exact lower bound is representable and must pass.
    assert list(timestamp_ns_for(0, 1, _INT64_MIN, 1)) == [_INT64_MIN]


def test_clock_guard_checks_the_first_sample_not_only_the_last() -> None:
    """A start inside int64 can still overflow once the sample offset is added."""
    with pytest.raises(ValueError, match="do not fit in int64"):
        timestamp_ns_for(1, 1, _INT64_MAX, 6)


def test_clock_guard_is_skipped_for_an_empty_chunk() -> None:
    """``count=0`` yields an empty int64 array even for an out-of-range start.

    ``docs/interface.md`` section 6 allows an empty in-memory chunk; no
    timestamp is produced, so no range applies and nothing may be rejected.
    """
    stamps = timestamp_ns_for(0, 0, _INT64_MAX + 1, 100)

    assert stamps.dtype == np.int64
    assert stamps.shape == (0,)


def test_non_dividing_sample_rate_keeps_half_up_rounding() -> None:
    """Rates that do not divide 1e9 must round half up, not truncate."""
    stamps = timestamp_ns_for(0, 4, 0, 7)

    assert list(stamps) == [0, 142_857_143, 285_714_286, 428_571_429]


def test_sample_rate_at_the_nanosecond_bound_is_exact() -> None:
    """One sample per nanosecond is the fastest clock ``int64`` can express."""
    stamps = timestamp_ns_for(0, 5, 0, MAX_SAMPLE_RATE_HZ)

    assert MAX_SAMPLE_RATE_HZ == NANOSECONDS_PER_SECOND
    assert list(stamps) == [0, 1, 2, 3, 4]
    assert bool(np.all(np.diff(stamps) == 1))


def test_sample_rate_above_the_nanosecond_bound_is_rejected() -> None:
    """A sub-nanosecond period cannot be represented, so it must be refused."""
    for rate in (MAX_SAMPLE_RATE_HZ + 1, 10**10, 9_223_372_034):
        with pytest.raises(ValueError, match="sample_rate_hz must be at most"):
            timestamp_ns_for(0, 1, 0, rate)


def test_former_overflow_rate_no_longer_returns_a_negative_timestamp() -> None:
    """Issue #46: ``9_223_372_034`` Hz used to wrap to ``-1000000001`` ns."""
    rate = 9_223_372_034
    remainder = rate - 1
    # The exact offset, computed in Python ints, is one whole nanosecond.
    assert (remainder * NANOSECONDS_PER_SECOND + rate // 2) // rate == (
        NANOSECONDS_PER_SECOND
    )

    with pytest.raises(ValueError, match="sample_rate_hz must be at most"):
        timestamp_ns_for(remainder, 1, 0, rate)


def test_scenario_above_the_nanosecond_bound_is_rejected() -> None:
    """``simulate_chunk`` must refuse the rate before generating any sample."""
    with pytest.raises(ValueError, match=r"scenario\.sample_rate_hz must be at most"):
        simulate_chunk(scenario(sample_rate_hz=MAX_SAMPLE_RATE_HZ + 1), 0, 16)


def test_largest_legal_rate_keeps_strictly_increasing_stamps() -> None:
    """The slowest representable period must not repeat a timestamp."""
    config = scenario(sample_rate_hz=MAX_SAMPLE_RATE_HZ, total_points=8, chunk_points=8)
    chunk = simulate_chunk(config, 0, 8)

    assert bool(np.all(np.diff(chunk.timestamp_ns) == 1))
    assert bool(np.all(np.diff(chunk.timestamp_ns) > 0))


def test_start_time_accepts_datetime_and_iso_string() -> None:
    """Both spellings of the start instant must give the same nanoseconds."""
    as_string = load_scenario_parameters(
        scenario(start_time_utc="2026-10-14T00:00:00Z")
    )
    as_object = load_scenario_parameters(
        scenario(start_time_utc=dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc))
    )

    assert as_string.start_time_utc_ns == as_object.start_time_utc_ns


def test_naive_start_time_is_treated_as_utc() -> None:
    """A timestamp without a zone must not be reinterpreted as local time."""
    naive = load_scenario_parameters(scenario(start_time_utc=dt.datetime(2026, 10, 14)))
    aware = load_scenario_parameters(scenario(start_time_utc="2026-10-14T00:00:00Z"))

    assert naive.start_time_utc_ns == aware.start_time_utc_ns


def test_bad_start_time_string_is_rejected() -> None:
    """An unparseable timestamp must name its field."""
    with pytest.raises(ValueError, match=r"scenario\.start_time_utc"):
        simulate_chunk(scenario(start_time_utc="not-a-timestamp"), 0, 16)


# --------------------------------------------------------------------------- #
# Noise sub-streams
# --------------------------------------------------------------------------- #


def test_noise_streams_are_independent() -> None:
    """Vibration and temperature must not share a random stream."""
    config = signals(
        vibration={"noise_std": 1.0},
        temperature={"noise_std": 1.0},
    )
    chunk = simulate_chunk(config, 0, 4096)

    vibration_residual = chunk.vibration - 0.5
    temperature_residual = chunk.temperature - 35.0
    correlation = float(np.corrcoef(vibration_residual, temperature_residual)[0, 1])

    assert abs(correlation) < 0.05


def test_stream_names_select_different_sequences() -> None:
    """The same index drawn from two stream names must differ."""
    vibration = gaussian_noise(42, STREAM_VIBRATION, 0, 64, 1.0)
    temperature = gaussian_noise(42, STREAM_TEMPERATURE, 0, 64, 1.0)

    assert not np.array_equal(vibration, temperature)


def test_gaussian_noise_scales_and_zero_shortcut() -> None:
    """``std`` scales the stream, and ``std=0`` returns exact zeros."""
    unit = gaussian_noise(42, STREAM_VIBRATION, 0, 256, 1.0)
    scaled = gaussian_noise(42, STREAM_VIBRATION, 0, 256, 2.5)
    zeros = gaussian_noise(42, STREAM_VIBRATION, 0, 256, 0.0)

    assert np.allclose(scaled, unit * 2.5, rtol=0.0, atol=0.0)
    assert np.array_equal(zeros, np.zeros(256))


def test_gaussian_noise_is_block_independent() -> None:
    """Random access across block boundaries must match sequential access."""
    sequential = gaussian_noise(7, STREAM_VIBRATION, 0, 50_000, 1.0)
    tail = gaussian_noise(7, STREAM_VIBRATION, 40_000, 10_000, 1.0)

    assert np.array_equal(tail, sequential[40_000:])


def test_zero_noise_scenario_is_exactly_deterministic() -> None:
    """A noise-free scenario must reproduce the trend bit for bit."""
    config = signals(
        vibration={"noise_std": 0.0},
        temperature={"noise_std": 0.0},
    )
    first = simulate_chunk(config, 0, 1000)
    second = simulate_chunk(config, 0, 1000)

    assert np.array_equal(first.vibration, second.vibration)
    assert bool(np.all(np.diff(first.vibration) > 0))


def test_high_degradation_rate_is_handled_without_overflow() -> None:
    """A steep but finite trend must stay finite over a long run."""
    config = signals(vibration={"degradation_rate_per_sample": 1e-3})
    chunk = simulate_chunk(config, 0, 200_000)

    assert bool(np.all(np.isfinite(chunk.vibration)))
    assert float(chunk.vibration[-1]) > float(chunk.vibration[0])


def test_negative_values_are_not_clamped() -> None:
    """A negative baseline must survive untouched, not be clipped to zero."""
    config = signals(
        vibration={
            "baseline": -2.0,
            "degradation_rate_per_sample": 0.5,
            "noise_std": 0.0,
        }
    )
    chunk = simulate_chunk(config, 0, 4)

    # Hand-computed: -2.0 + 0.5 * i for i in 0..3, with zero noise.
    assert list(chunk.vibration) == [-2.0, -1.5, -1.0, -0.5]
    assert bool(np.all(chunk.vibration < 0.0))


# --------------------------------------------------------------------------- #
# Lower-level building blocks
# --------------------------------------------------------------------------- #


def test_linear_trend_is_a_pure_vectorised_function() -> None:
    """The trend helper must be exact, correctly typed and loop-free."""
    trend = linear_trend(0, 5, 1.0, 0.5)

    assert trend.dtype == np.float64
    assert list(trend) == [1.0, 1.5, 2.0, 2.5, 3.0]
    assert linear_trend(100, 0, 1.0, 0.5).shape == (0,)


def test_linear_trend_rejects_negative_arguments() -> None:
    """Negative indices and counts are invalid."""
    with pytest.raises(ValueError, match="start_seq must be non-negative"):
        linear_trend(-1, 4, 0.0, 0.0)
    with pytest.raises(ValueError, match="count must be non-negative"):
        linear_trend(0, -4, 0.0, 0.0)


def test_signal_parameters_from_values_validates() -> None:
    """The parameter object must reject bad input and accept good input."""
    good = SignalParameters.from_values(0.5, 1e-6, 0.05, "g")
    assert good.baseline == 0.5
    assert good.unit == "g"

    with pytest.raises(ValueError, match="must be non-negative"):
        SignalParameters.from_values(0.5, 1e-6, -1.0)
    with pytest.raises(ValueError, match="must be finite"):
        SignalParameters.from_values(0.5, float("inf"), 0.05)
    with pytest.raises(ValueError, match="unit must be a string"):
        SignalParameters.from_values(0.5, 1e-6, 0.05, 7)


def test_chunk_validate_catches_a_broken_contract() -> None:
    """``validate`` is the producer-facing guard for the frozen shard contract."""
    chunk = simulate_chunk(DEFAULT_SCENARIO, 0, 64)
    chunk.validate()

    broken = SimulationChunk(
        seq=chunk.seq.copy(),
        timestamp_ns=chunk.timestamp_ns.copy(),
        vibration=chunk.vibration.copy(),
        temperature=chunk.temperature[:32],
    )
    with pytest.raises(ValueError, match="equal length"):
        broken.validate()

    non_contiguous = SimulationChunk(
        seq=np.array([0, 1, 3, 4], dtype=np.int64),
        timestamp_ns=np.arange(4, dtype=np.int64),
        vibration=np.zeros(4),
        temperature=np.zeros(4),
    )
    with pytest.raises(ValueError, match="contiguous"):
        non_contiguous.validate()


def test_flat_configuration_layout_is_supported() -> None:
    """A flat config object (no nested ``scenario``/``signals`` blocks) must work."""
    flat = dict(DEFAULT_SCENARIO["scenario"])
    flat["vibration"] = dict(DEFAULT_SCENARIO["signals"]["vibration"])
    flat["temperature"] = dict(DEFAULT_SCENARIO["signals"]["temperature"])

    chunk = simulate_chunk(flat, 0, 32)
    nested = simulate_chunk(DEFAULT_SCENARIO, 0, 32)

    assert np.array_equal(chunk.vibration, nested.vibration)
    assert np.array_equal(chunk.temperature, nested.temperature)
    assert np.array_equal(chunk.timestamp_ns, nested.timestamp_ns)
