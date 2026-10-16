"""Unit tests for the G15 degradation trend module (Issue #5, owner B1).

These tests pin the deterministic half of the model frozen in
``docs/interface.md`` section 7.1: ``signal(i) = baseline + rate * i``.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from g15.simulation.degradation import (
    TEMPERATURE_UNIT,
    VIBRATION_UNIT,
    SignalParameters,
    linear_trend,
)


def test_linear_trend_matches_closed_form() -> None:
    """``linear_trend`` must equal ``baseline + rate * seq`` for every index."""
    start, count, baseline, rate = 17, 500, 2.5, 1e-4
    trend = linear_trend(start, count, baseline, rate)
    expected = baseline + rate * np.arange(start, start + count, dtype=np.float64)

    assert trend.dtype == np.float64
    assert np.array_equal(trend, expected)


def test_linear_trend_starts_at_baseline_for_index_zero() -> None:
    """Index ``0`` must evaluate to the baseline exactly."""
    trend = linear_trend(0, 3, 35.0, 1e-3)

    assert float(trend[0]) == 35.0
    assert float(trend[1]) == 35.001
    assert float(trend[2]) == 35.002


def test_linear_trend_is_flat_for_zero_rate() -> None:
    """A zero rate must return a constant array."""
    trend = linear_trend(1234, 64, -7.5, 0.0)

    assert bool(np.all(trend == -7.5))


def test_linear_trend_handles_empty_and_boundary_ranges() -> None:
    """Empty requests and large offsets must not misbehave."""
    assert linear_trend(0, 0, 1.0, 1.0).shape == (0,)
    assert linear_trend(999_999, 1, 0.0, 1e-6)[0] == pytest.approx(0.999999)


def test_linear_trend_does_not_drift_on_large_indices() -> None:
    """The exactness check that motivated integer ``seq`` indexing."""
    trend = linear_trend(1_000_000, 3, 0.0, 1e-6)

    assert float(trend[0]) == pytest.approx(1.0)
    assert float(trend[1]) == pytest.approx(1.000001)
    assert float(trend[2]) == pytest.approx(1.000002)


def test_linear_trend_rejects_negative_arguments() -> None:
    """Invalid index arguments must raise ``ValueError``."""
    with pytest.raises(ValueError, match="start_seq must be non-negative"):
        linear_trend(-1, 8, 0.0, 0.0)
    with pytest.raises(ValueError, match="count must be non-negative"):
        linear_trend(0, -8, 0.0, 0.0)


def test_signal_parameters_accepts_valid_values() -> None:
    """A well-formed channel configuration must round-trip unchanged."""
    params = SignalParameters.from_values(0.5, 1e-6, 0.05, VIBRATION_UNIT)

    assert params.baseline == 0.5
    assert params.degradation_rate_per_sample == 1e-6
    assert params.noise_std == 0.05
    assert params.unit == VIBRATION_UNIT == "g"


def test_signal_parameters_defaults_unit_to_empty() -> None:
    """``unit`` is optional metadata and must default to an empty string."""
    params = SignalParameters.from_values(35.0, 3e-6, 0.15)

    assert params.unit == ""
    assert TEMPERATURE_UNIT == "degC"


def test_signal_parameters_rejects_negative_rate_and_deviation() -> None:
    """Rates and deviations are magnitudes and must be non-negative."""
    with pytest.raises(
        ValueError, match=r"degradation_rate_per_sample must be non-negative"
    ):
        SignalParameters.from_values(0.5, -1e-6, 0.05)
    with pytest.raises(ValueError, match=r"noise_std must be non-negative"):
        SignalParameters.from_values(0.5, 1e-6, -0.05)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_signal_parameters_rejects_non_finite_numbers(bad: float) -> None:
    """NaN and infinities must be refused at construction time."""
    with pytest.raises(ValueError, match="must be finite"):
        SignalParameters.from_values(bad, 1e-6, 0.05)
    with pytest.raises(ValueError, match="must be finite"):
        SignalParameters.from_values(0.5, 1e-6, bad)


@pytest.mark.parametrize("bad", ["abc", None, [1]])
def test_signal_parameters_rejects_non_numeric(bad: object) -> None:
    """Non-numeric baselines must be rejected with a clear message."""
    with pytest.raises(ValueError, match="must be a finite number"):
        SignalParameters.from_values(bad, 1e-6, 0.05)


def test_signal_parameters_rejects_non_string_unit() -> None:
    """The unit field is descriptive text, not a number."""
    with pytest.raises(ValueError, match="unit must be a string"):
        SignalParameters.from_values(0.5, 1e-6, 0.05, 1)

    assert SignalParameters.from_values(0.5, 1e-6, 0.05, None).unit == ""


def test_signal_parameters_accepts_integer_valued_floats() -> None:
    """YAML often yields ints; they must be accepted as finite numbers."""
    params = SignalParameters.from_values(1, 0, 0)

    assert params.baseline == 1.0
    assert params.degradation_rate_per_sample == 0.0
    assert params.noise_std == 0.0


def test_signal_parameters_is_frozen() -> None:
    """The parameter object must be immutable once validated."""
    params = SignalParameters.from_values(0.5, 1e-6, 0.05, "g")

    with pytest.raises(Exception):
        params.baseline = 9.0  # type: ignore[misc]


def test_trend_is_not_clamped_for_negative_values() -> None:
    """Oscillation about the baseline is physical; values must not be clipped."""
    trend = linear_trend(0, 3, -1.0, 0.0)

    assert math.isclose(float(trend[0]), -1.0)
    assert bool(np.all(trend < 0.0))
