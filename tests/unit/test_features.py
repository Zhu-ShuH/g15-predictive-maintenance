"""Unit tests for windowed feature extraction (Issue #7, owner B2).

The expectations are computed by hand or taken from a textbook identity, never
from a second copy of the implementation: a sine at ``f`` Hz must report ``f``,
a unit square wave has RMS 1.0, a two-point symmetric window has excess kurtosis
``-2``. Boundary inputs (constant, all-zero, one sample, NaN/Inf) must return
finite numbers or raise, and must never return ``NaN``/``Inf``.
"""

from __future__ import annotations

import math
import re

import numpy as np
import pytest

from g15.analysis.features import (
    FEATURES_VERSION,
    FEATURE_NAMES,
    FeatureRecord,
    extract_features,
)


def _sine(freq_hz: float, sample_rate_hz: int, count: int) -> np.ndarray:
    """Return a pure sine of amplitude 1 at ``freq_hz``."""
    index = np.arange(count, dtype=np.float64)
    return np.sin(2.0 * math.pi * freq_hz * index / sample_rate_hz)


def test_contract_names_cover_six_required_features() -> None:
    """Section 8.2 requires at least mean/std/RMS/ptp/kurtosis/dominant freq."""
    required = {"mean", "std", "rms", "ptp", "kurtosis_excess", "dominant_freq_hz"}
    assert required.issubset(set(FEATURE_NAMES))
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))
    assert FEATURES_VERSION


def test_hand_computed_moments() -> None:
    """[0, 1, -1, 2] has mean 0.5, population variance 1.25, RMS sqrt(1.5)."""
    samples = np.array([0.0, 1.0, -1.0, 2.0])
    record = extract_features(samples, 100)

    assert record.mean == pytest.approx(0.5)
    assert record.std == pytest.approx(math.sqrt(1.25))
    assert record.rms == pytest.approx(math.sqrt(1.5))
    assert record.ptp == pytest.approx(3.0)
    assert record.crest_factor == pytest.approx(2.0 / math.sqrt(1.5))
    assert record.sample_count == 4
    assert record.sample_rate_hz == 100
    assert record.window_start_seq is None


def test_excess_kurtosis_of_two_point_window_is_minus_two() -> None:
    """A symmetric two-point window has m4/m2**2 == 1, so excess kurtosis -2."""
    samples = np.array([1.0, -1.0, 1.0, -1.0])
    assert extract_features(samples, 10).kurtosis_excess == pytest.approx(-2.0)


def test_zero_crossing_rate_of_alternating_window_is_one() -> None:
    """Every consecutive pair of [1, -1, 1, -1] crosses the mean."""
    samples = np.array([1.0, -1.0, 1.0, -1.0])
    assert extract_features(samples, 10).zero_crossing_rate == pytest.approx(1.0)


def test_dc_offset_does_not_suppress_zero_crossing_rate() -> None:
    """The mean is removed first, so a large offset keeps the same crossings."""
    shifted = np.array([101.0, 99.0, 101.0, 99.0])
    assert extract_features(shifted, 10).zero_crossing_rate == pytest.approx(1.0)


def test_constant_window_is_finite_and_flat() -> None:
    """A constant window must not divide by zero anywhere."""
    samples = np.full(256, 2.0)
    record = extract_features(samples, 100)

    assert record.mean == pytest.approx(2.0)
    assert record.std == pytest.approx(0.0)
    assert record.rms == pytest.approx(2.0)
    assert record.ptp == pytest.approx(0.0)
    assert record.kurtosis_excess == pytest.approx(0.0)
    assert record.crest_factor == pytest.approx(1.0)
    assert record.dominant_freq_hz == pytest.approx(0.0)
    assert record.zero_crossing_rate == pytest.approx(0.0)


def test_all_zero_window_returns_zero_crest_factor() -> None:
    """RMS is zero here, so the division must be guarded instead of yielding inf."""
    record = extract_features(np.zeros(16), 100)
    assert record.crest_factor == 0.0
    assert record.rms == 0.0
    assert record.dominant_freq_hz == 0.0


def test_single_sample_window_is_allowed() -> None:
    """One sample has no shape: std, kurtosis, frequency and ZCR are zero."""
    record = extract_features(np.array([3.0]), 100)
    assert record.mean == pytest.approx(3.0)
    assert record.std == 0.0
    assert record.kurtosis_excess == 0.0
    assert record.dominant_freq_hz == 0.0
    assert record.zero_crossing_rate == 0.0


def test_sine_reports_its_own_frequency() -> None:
    """A 10 Hz sine sampled at 100 Hz over 100 samples lands exactly on bin 10."""
    record = extract_features(_sine(10.0, 100, 100), 100)
    assert record.dominant_freq_hz == pytest.approx(10.0)
    assert record.rms == pytest.approx(1.0 / math.sqrt(2.0), rel=1e-3)


def test_amplitude_of_a_sine_whose_samples_hit_the_peaks() -> None:
    """At 25 Hz every fourth sample is a peak, so ptp is exactly 2 and RMS 1/sqrt(2).

    25 Hz is also an exact bin (``25 * 100 / 100``), so no leakage moves the peak.
    """
    record = extract_features(_sine(25.0, 100, 100), 100)
    assert record.ptp == pytest.approx(2.0, rel=1e-12)
    assert record.rms == pytest.approx(1.0 / math.sqrt(2.0), rel=1e-3)
    assert record.dominant_freq_hz == pytest.approx(25.0)


def test_dominant_frequency_excludes_dc() -> None:
    """A 5 g offset must not hide the 10 Hz component (DC is removed first)."""
    samples = 5.0 + _sine(10.0, 100, 100)
    assert extract_features(samples, 100).dominant_freq_hz == pytest.approx(10.0)


def test_nyquist_component_is_reported_not_doubled_away() -> None:
    """Alternating samples are the Nyquist bin; the even-length path must keep it."""
    samples = np.array([1.0, -1.0] * 50)
    assert extract_features(samples, 100).dominant_freq_hz == pytest.approx(50.0)


@pytest.mark.parametrize(
    "bad",
    [
        np.array([1.0, np.nan, 2.0]),
        np.array([1.0, np.inf, 2.0]),
        np.array([1.0, -np.inf, 2.0]),
    ],
)
def test_non_finite_samples_are_rejected(bad: np.ndarray) -> None:
    """A silent NaN would poison the score, so it must raise with the index."""
    with pytest.raises(ValueError, match="not finite at local index 1"):
        extract_features(bad, 100)


def test_empty_window_is_rejected() -> None:
    """An empty window has no features; the consumer must never store one."""
    with pytest.raises(ValueError, match="must not be empty"):
        extract_features(np.array([], dtype=np.float64), 100)


def test_two_dimensional_input_is_rejected() -> None:
    """A matrix is not a window."""
    with pytest.raises(ValueError, match="one-dimensional"):
        extract_features(np.zeros((2, 2)), 100)


def test_non_array_input_is_rejected() -> None:
    """Lists are rejected so a caller cannot silently skip the dtype checks."""
    with pytest.raises(ValueError, match="numpy array"):
        extract_features([0.1, 0.2], 100)  # type: ignore[arg-type]


def test_non_numeric_dtype_is_rejected() -> None:
    """String arrays cannot be analysed."""
    with pytest.raises(ValueError, match="numeric"):
        extract_features(np.array(["a", "b"]), 100)


@pytest.mark.parametrize(
    "window",
    [
        np.array([1, 2], dtype="timedelta64[D]"),
        np.array([1, 2], dtype="timedelta64[s]"),
        np.array(["2026-01-01", "2026-01-02"], dtype="datetime64[s]"),
    ],
    ids=["timedelta-days", "timedelta-seconds", "datetime"],
)
def test_time_like_dtypes_are_rejected(window: np.ndarray) -> None:
    """NumPy counts ``timedelta64`` as a number, so a generic test accepts it.

    ``extract_features(np.array([1, 2], dtype="timedelta64[D]"), 100)`` used to
    return ``mean=1.5, ptp=1.0``: one day became one ``g``, and the same physical
    duration written in seconds produced a different feature vector. The accepted
    dtypes are listed explicitly now, and the refusal names the dtype.
    """
    with pytest.raises(ValueError, match="real numeric dtype"):
        extract_features(window, 100)

    with pytest.raises(ValueError, match=re.escape(str(window.dtype))):
        extract_features(window, 100)


def test_boolean_window_is_rejected() -> None:
    """``True`` is not a vibration sample even though it converts to 1.0."""
    with pytest.raises(ValueError, match="real numeric dtype"):
        extract_features(np.array([True, False, True]), 100)


@pytest.mark.parametrize(
    "window",
    [
        np.array([2**64 - 1, 2**64 - 2], dtype=np.uint64),
        np.array([2**63 - 1, 2**63 - 2], dtype=np.int64),
        np.array([2**53, 2**53 + 1], dtype=np.int64),
        np.array([-(2**53) - 1, -(2**53) - 2], dtype=np.int64),
    ],
    ids=["uint64-max", "int64-max", "just-above-2**53", "negative-above-2**53"],
)
def test_integer_window_that_float64_cannot_hold_is_rejected(
    window: np.ndarray,
) -> None:
    """Issue #66 R2: the implicit cast to ``float64`` erased a real difference.

    ``[2**64 - 1, 2**64 - 2]`` used to be accepted with ``std=0.0`` and
    ``ptp=0.0`` even though the two samples differ by one, because both rounded
    to the same ``float64``. The window is refused with the dtype and the value
    named instead of silently reporting a flat signal.
    """
    with pytest.raises(ValueError, match="exactly representable"):
        extract_features(window, 100)

    with pytest.raises(ValueError, match=re.escape(str(window.dtype))):
        extract_features(window, 100)


@pytest.mark.parametrize(
    "window",
    [
        np.array([10**15, 10**15 + 1], dtype=np.int64),
        np.array([2**31 - 1, 2**31 - 2], dtype=np.int32),
        np.array([2**32 - 1, 2**32 - 2], dtype=np.uint32),
        np.array([-(10**12), 10**12], dtype=np.int64),
    ],
    ids=["1e15", "int32-max", "uint32-max", "symmetric-1e12"],
)
def test_integer_window_that_float64_holds_exactly_is_accepted(
    window: np.ndarray,
) -> None:
    """The rejection is about exact representation, not about a blanket range.

    These windows are large but their midpoints are still ``float64`` values, so
    the whole two-point identity holds: ``ptp`` is the integer spread and ``std``
    is half of it.
    """
    record = extract_features(window, 100)
    spread = float(int(window.max()) - int(window.min()))

    assert record.ptp == spread
    assert record.std == spread / 2.0


@pytest.mark.parametrize(
    "window",
    [
        np.array([2**53, 2**53 - 1], dtype=np.int64),
        np.array([2**53 + 2, 2**53 + 4], dtype=np.int64),
    ],
    ids=["at-2**53", "even-above-2**53"],
)
def test_integers_near_the_float64_boundary_keep_their_spread(
    window: np.ndarray,
) -> None:
    """Exact samples are not the same promise as exact statistics.

    Above ``2**52`` the spacing of ``float64`` reaches ``2``, so the midpoint of
    two exactly held samples need not exist: ``2**53 - 0.5`` has no ``float64``
    value, and ``std`` of ``[2**53 + 2, 2**53 + 4]`` comes out ``1.414...``
    instead of ``1.0``. ``ptp`` is immune because it is a difference of values
    that are held exactly.

    So the contract drawn here is only "the conversion loses nothing" -- the
    window is not silently flattened to ``ptp=0`` the way ``[2**64-1, 2**64-2]``
    was -- and this test pins that line rather than claiming moments stay exact
    at 9e15 ``g``, which no ``float64`` computation can deliver.
    """
    record = extract_features(window, 100)
    spread = float(int(window.max()) - int(window.min()))

    assert record.ptp == spread
    assert record.ptp > 0.0
    assert math.isfinite(record.std)


@pytest.mark.parametrize("dtype", ["int8", "int16", "float16", "float32", "float64"])
def test_ordinary_real_dtypes_are_accepted(dtype: str) -> None:
    """Widening to ``float64`` is lossless for these, so the features must agree."""
    record = extract_features(np.array([1, 2, 3], dtype=dtype), 100)

    assert record.mean == pytest.approx(2.0)
    assert record.std == pytest.approx(math.sqrt(2.0 / 3.0))
    assert record.ptp == pytest.approx(2.0)


def test_features_version_marks_the_narrowed_input_domain() -> None:
    """``1.1`` is ``1.0`` with a narrower domain and defined extreme results.

    The eight formulas are unchanged; the accepted inputs are not, so a stored
    feature vector cannot be attributed to this module without the version.
    """
    assert FEATURES_VERSION == "1.1"


def test_ordinary_windows_match_the_plain_float64_formulas() -> None:
    """Compatibility boundary for ``1.0`` -> ``1.1``, stated as a tolerance.

    The rescaling divides by a power of two, which is exact except where a sample
    becomes subnormal, so ordinary windows keep their numbers to floating-point
    rounding. They are *not* promised to be bit-identical: a 400-window sweep of
    this recipe against ``1.0`` differed by one ulp of ``kurtosis_excess`` on one
    window under NumPy 2.3.5 and was identical under NumPy 2.5.4. Recomputing the
    same statistics directly from the samples is therefore the honest check --
    it pins the formulas without pinning the last bit.
    """
    rng = np.random.default_rng(42)
    sizes = (1, 2, 3, 7, 64, 256, 1024)
    compared = 0

    for index in range(400):
        count = sizes[index % len(sizes)]
        amplitude = 10.0 ** int(rng.integers(-8, 9))
        samples = (rng.normal(0.0, 1.0, count) + rng.uniform(-5.0, 5.0)) * amplitude
        record = extract_features(samples, 100)

        centered = samples - samples.mean()
        variance = float(np.mean(centered**2))
        assert record.mean == pytest.approx(float(samples.mean()), rel=1e-15)
        assert record.std == pytest.approx(math.sqrt(variance), rel=1e-15)
        assert record.rms == pytest.approx(
            math.sqrt(float(np.mean(samples**2))), rel=1e-15
        )
        assert record.ptp == pytest.approx(
            float(samples.max() - samples.min()), rel=1e-15
        )
        if variance > 0.0:
            assert record.kurtosis_excess == pytest.approx(
                float(np.mean(centered**4)) / variance**2 - 3.0,
                rel=1e-12,
                abs=1e-12,
            )
            compared += 1

    # A sweep that silently degenerated to one-sample windows would prove nothing.
    assert compared > 300


@pytest.mark.parametrize("rate", [0, -1, 1_000_000_001])
def test_invalid_sample_rate_is_rejected(rate: int) -> None:
    """Section 5.1 bounds the rate to 1..1e9; the analysis layer keeps the bound."""
    with pytest.raises(ValueError, match="sample_rate_hz"):
        extract_features(np.zeros(4), rate)


def test_boolean_sample_rate_is_rejected() -> None:
    """``True`` is not a sampling rate even though it equals 1."""
    with pytest.raises(ValueError, match="must be an integer"):
        extract_features(np.zeros(4), True)  # type: ignore[arg-type]


def test_window_start_seq_is_recorded() -> None:
    """The alarm layer needs the window position, so it must survive extraction."""
    record = extract_features(np.zeros(4), 100, window_start_seq=16384)
    assert record.window_start_seq == 16384


def test_negative_window_start_seq_is_rejected() -> None:
    """A negative position cannot name a window of a run."""
    with pytest.raises(ValueError, match="non-negative"):
        extract_features(np.zeros(4), 100, window_start_seq=-256)


def test_float32_input_is_converted_and_input_untouched() -> None:
    """Casting happens on a copy, so the caller's array keeps its dtype and values."""
    samples = np.array([0.5, 0.25, -0.75], dtype=np.float32)
    original = samples.copy()
    record = extract_features(samples, 100)

    assert record.rms == pytest.approx(
        float(np.sqrt(np.mean(samples.astype(float) ** 2)))
    )
    assert samples.dtype == np.float32
    np.testing.assert_array_equal(samples, original)


def test_feature_value_and_as_mapping_agree() -> None:
    """Both accessors must expose the same eight numbers as ``float``."""
    record = extract_features(_sine(10.0, 100, 100), 100)
    mapping = record.as_mapping()

    assert set(mapping) == set(FEATURE_NAMES)
    for name, value in mapping.items():
        assert isinstance(value, float)
        assert record.feature_value(name) == value


def test_feature_value_rejects_unknown_name() -> None:
    """A typo must raise ``KeyError`` so the health layer can report a path."""
    record = extract_features(np.zeros(4), 100)
    with pytest.raises(KeyError):
        record.feature_value("rms_db")


def test_record_is_immutable() -> None:
    """Downstream code cannot edit a window that has already been stored."""
    record = extract_features(np.zeros(4), 100)
    with pytest.raises(AttributeError):
        record.rms = 1.0  # type: ignore[misc]


def test_feature_record_can_be_built_directly_for_scoring_tests() -> None:
    """The health layer's tests need to fabricate a vector; keep that possible."""
    record = FeatureRecord(
        mean=0.5,
        std=0.05,
        rms=0.5025,
        ptp=0.28,
        kurtosis_excess=0.0,
        crest_factor=1.27,
        dominant_freq_hz=25.0,
        zero_crossing_rate=0.5,
        sample_count=256,
        sample_rate_hz=100,
        window_start_seq=0,
    )
    assert record.as_mapping()["rms"] == 0.5025


def test_complex_window_is_rejected() -> None:
    """Issue #66: casting complex to float64 only warned and dropped ``imag``.

    Accepting the window computed ``mean=2.0`` for ``[1+2j, 3+4j]``, which
    describes a signal nobody passed in, so the entry point must refuse it.
    """
    with pytest.raises(ValueError, match="must be real-valued"):
        extract_features(np.array([1 + 2j, 3 + 4j]), 100, window_start_seq=0)


def test_complex_window_is_rejected_before_any_number_is_returned() -> None:
    """The rejection must not depend on the window being otherwise well formed."""
    clean = np.array([1.0, -1.0, 1.0, -1.0], dtype=np.complex128)
    with pytest.raises(ValueError, match="complex128"):
        extract_features(clean, 100)


@pytest.mark.parametrize("magnitude", [1e100, 1e150, 1e200, 1e300])
def test_large_finite_window_returns_finite_features(magnitude: float) -> None:
    """Issue #66: ``[1e100, -1e100]`` used to return a ``NaN`` excess kurtosis.

    The window is rescaled by a power of two before the moments are formed, so
    every feature stays finite and the shape features keep their exact values:
    two symmetric points have ``ptp = 2m``, ``std = rms = m`` and excess
    kurtosis ``-2`` at any magnitude.
    """
    record = extract_features(np.array([magnitude, -magnitude]), 100)

    assert record.mean == pytest.approx(0.0)
    assert record.std == pytest.approx(magnitude)
    assert record.rms == pytest.approx(magnitude)
    assert record.ptp == pytest.approx(2.0 * magnitude)
    assert record.crest_factor == pytest.approx(1.0)
    assert record.kurtosis_excess == pytest.approx(-2.0)
    for value in record.as_mapping().values():
        assert math.isfinite(value)


@pytest.mark.parametrize("exponent", [-300, -150, -3, 0, 3, 150, 300])
def test_features_are_finite_across_signal_magnitudes(exponent: int) -> None:
    """A pure sine keeps its shape and finite numbers at any scale.

    25 Hz sampled at 100 Hz puts every fourth sample on a peak, so ``ptp`` is
    exactly ``2 * scale``; the values of a sampled sine (``rms = 1/sqrt(2)``,
    excess kurtosis ``-1``) must survive the rescaling unchanged.
    """
    scale = 10.0**exponent
    record = extract_features(_sine(25.0, 100, 100) * scale, 100)

    assert record.dominant_freq_hz == pytest.approx(25.0)
    assert record.ptp == pytest.approx(2.0 * scale, rel=1e-12)
    assert record.rms == pytest.approx(scale / math.sqrt(2.0), rel=1e-12)
    assert record.kurtosis_excess == pytest.approx(-1.0, rel=1e-9)
    for value in record.as_mapping().values():
        assert math.isfinite(value)


def test_window_whose_spread_exceeds_float64_is_rejected() -> None:
    """``+/-1.7e308`` is finite but its peak-to-peak is ``3.4e308``.

    Returning ``inf`` would hand the score and the alarm state machine a number
    that is not a measurement, so the field name is reported instead.
    """
    with pytest.raises(ValueError, match="ptp exceeds the float64 range"):
        extract_features(np.array([1.7e308, -1.7e308]), 100)


def test_rescaled_path_does_not_modify_the_input() -> None:
    """The power-of-two division must happen on a copy, like the ``float64`` cast."""
    samples = np.array([1e100, -1e100, 3e100], dtype=np.float64)
    original = samples.copy()
    extract_features(samples, 100)

    assert samples.dtype == np.float64
    np.testing.assert_array_equal(samples, original)


def test_tiny_window_keeps_its_moments_instead_of_underflowing_to_zero() -> None:
    """The rescaling also fixes the other end: ``1e-300`` squared underflowed.

    ``std``, ``rms`` and ``crest_factor`` used to be ``0.0`` here even though the
    window has spread; the rescaled computation reports the real values.
    """
    samples = np.array([1e-300, -1e-300, 3e-300])
    record = extract_features(samples, 100)

    assert record.std > 0.0
    assert record.rms > 0.0
    assert record.crest_factor > 0.0
    assert math.isfinite(record.kurtosis_excess)


def test_window_inside_the_rescale_range_is_not_rescaled() -> None:
    """``max|sample|`` in ``(1, 2]`` yields factor 1, so no bit may move.

    Ordinary ``g`` windows land here. Pinning the exact values guards against a
    later change that rescales unconditionally and shifts the last bit of every
    stored feature.
    """
    samples = np.array([1.5, -0.5, 0.25])
    record = extract_features(samples, 100)
    centered = samples - samples.mean()

    assert record.mean == float(samples.mean())
    assert record.std == math.sqrt(float(np.mean(centered**2)))
    assert record.rms == math.sqrt(float(np.mean(samples**2)))
    assert record.ptp == float(samples.max() - samples.min())
