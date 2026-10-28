"""Windowed vibration features for the G15 analysis layer (Issue #7, owner B2).

The module implements ``docs/interface.md`` section 8.2: one window of raw
``vibration`` samples is turned into a small, fully described feature vector.
Everything here is a pure function of the samples; no file is read, no clock is
consulted and no fault truth is visible, which is what keeps the detector honest
under section 5.3.

Contract decisions that callers must know:

* **Statistics are population statistics** (``ddof=0``). A window is treated as
  the whole population of interest, not as a sample of a larger one; this also
  keeps ``std`` finite for a one-sample window instead of returning ``NaN``.
* **``kurtosis_excess`` uses the Fisher definition** (normal noise is ≈ 0) with
  the biased moment estimator ``m4 / m2**2 - 3``. A constant window has no
  defined excess kurtosis; ``0.0`` is reported and the reason is documented
  here instead of leaking ``NaN`` downstream.
* **``dominant_freq_hz`` excludes DC.** The window mean is removed before the
  real FFT, so the strongest *oscillating* component wins. The one-sided
  amplitude is ``2*|X_k|/n`` for interior bins and ``|X_k|/n`` for the DC and
  Nyquist bins, following the usual even-length ``rfft`` normalization.
* **Every division is guarded.** Zero variance, an all-zero window and a
  one-sample window return ``0.0`` for the affected feature rather than
  ``NaN``/``Inf``, so the consumer may store any window it can produce.
* **Real inputs only, from an explicit list of dtypes.** A complex window is
  rejected instead of being cast to ``float64``: that cast raises a warning and
  silently drops the imaginary part, so the returned features would describe
  data the caller never passed. ``np.issubdtype(dtype, np.number)`` is *not*
  used as the gate, because NumPy counts ``timedelta64`` as a number: a window
  of ``timedelta64[D]`` would be read as a vibration in ``g`` and the same
  physical duration in ``[s]`` would give a different feature vector. The
  supported set is listed in :data:`REAL_SAMPLE_DTYPES` and anything else is
  rejected with its dtype named.
* **Integers must survive the conversion to ``float64``.** An integer window is
  converted before the moments are formed, and ``[2**64-1, 2**64-2]`` collapses
  to one ``float64`` value, so ``std`` and ``ptp`` would both be ``0.0`` for two
  samples that differ by one. A window is therefore accepted only when every
  sample has an exact ``float64`` value: everything inside
  ``|value| <= 2**53`` does, and past that only the values that land on the
  widened spacing (``2**53 + 2`` yes, ``2**53 + 1`` no). The ``float64`` shards
  the consumer reads never reach this path.
* **Moments are formed on a rescaled window.** The window is divided by a power
  of two before the moments are computed, so the largest rescaled magnitude
  stays in ``(1, 2)`` (the peak is ``1.0`` for an all-zero window) and
  ``centered**4`` stays below ``256`` instead of overflowing for
  large-but-finite samples (``1e100`` used to return a ``NaN`` excess kurtosis,
  ``1e200`` an ``inf`` RMS). Features are scaled back before they are returned;
  a feature that still cannot be represented as a finite ``float64`` makes the
  extraction raise instead of returning ``Inf``.

  Dividing by a power of two is exact for every finite input except where a
  sample is pushed into the subnormal range, so ordinary windows keep their
  numbers in practice: a 400-window sweep over ordinary magnitudes (see
  :data:`FEATURES_VERSION`) matched ``1.0`` bit for bit on NumPy 2.5.4 and
  differed by a single ulp of ``kurtosis_excess`` on one window under NumPy
  2.3.5. That is a rounding difference, not a formula change -- but "identical
  for every ordinary input" is not claimed, which is why the version moved to
  ``1.1``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, NamedTuple

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "FEATURES_VERSION",
    "FEATURE_NAMES",
    "REAL_SAMPLE_DTYPES",
    "FeatureRecord",
    "extract_features",
]

#: Version of the feature definitions in this module.
#:
#: ``1.1`` is ``1.0`` with a narrower accepted input domain and defined results
#: at the extremes; the eight formulas themselves are unchanged.
#:
#: What moved, stated precisely because "unchanged for ordinary inputs" is easy
#: to over-read:
#:
#: * complex windows are rejected instead of being cast (``1.0`` silently kept
#:   the real part and warned);
#: * ``timedelta64`` windows are rejected instead of being read as ``g``;
#: * integer windows outside ``|value| <= 2**53`` are rejected instead of being
#:   rounded into ``float64``;
#: * very large finite windows return finite numbers instead of ``NaN``/``Inf``
#:   (``1e100`` used to return a ``NaN`` excess kurtosis, ``1e200`` an ``inf``
#:   RMS), and windows whose spread exceeds ``float64`` raise;
#: * no window that ``1.0`` returned finite numbers for changed formula, but
#:   bit-identity is not guaranteed: of 400 ordinary windows compared against
#:   ``1.0`` on NumPy 2.3.5, one differed by one ulp of ``kurtosis_excess``
#:   (``-0.18370102779317943`` -> ``-0.18370102779317987``); the same sweep was
#:   bit-identical on NumPy 2.5.4. Treat ``1.0``/``1.1`` values as equal within
#:   floating-point rounding, not as identical bytes.
#:
#: Scope, stated because it is easy to over-read: this constant is **not** a
#: field of :class:`FeatureRecord` and currently has no persistence path. It is
#: module metadata for logs and run manifests only. Storing a per-row version
#: would add a column to ``feature_windows``, which is a schema decision still
#: open in issue #57 -- do not assume a stored row can be traced by itself until
#: that issue closes.
FEATURES_VERSION: Final[str] = "1.1"

#: dtypes a vibration window may carry, in the order they are documented.
#:
#: Deliberately a whitelist: ``np.number`` also covers ``timedelta64`` and
#: ``datetime64``, so a generic "is it a number" test accepts a window of time
#: deltas and reports its day counts as ``g``. ``float16`` is included because
#: every one of its values is a ``float64`` value, so the widening is lossless.
#: ``longdouble`` is not: on the platforms where it is wider than ``float64``
#: the conversion drops digits silently.
REAL_SAMPLE_DTYPES: Final[tuple[np.dtype, ...]] = tuple(
    np.dtype(name)
    for name in (
        "float16",
        "float32",
        "float64",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
    )
)

#: Magnitude every integer sample is exact at; the bound the fast path uses.
#:
#: ``float64`` represents every integer up to ``2**53`` exactly. Past it the
#: spacing exceeds one, so ``[2**53, 2**53 + 1]`` becomes two identical values
#: and the window's real spread disappears before any moment is formed. Some
#: larger integers (``2**53 + 2``) are still exact, so this constant is the
#: guaranteed range, not the acceptance limit: windows that reach past it are
#: checked value by value in :func:`_require_exact_integers`.
MAX_EXACT_INTEGER: Final[int] = 2**53

#: Feature names in reporting order.
FEATURE_NAMES: Final[tuple[str, ...]] = (
    "mean",
    "std",
    "rms",
    "ptp",
    "kurtosis_excess",
    "crest_factor",
    "dominant_freq_hz",
    "zero_crossing_rate",
)

#: Upper bound of ``sample_rate_hz`` from ``docs/interface.md`` section 5.1.
#: Re-declared here so the analysis layer does not import the simulator.
MAX_SAMPLE_RATE_HZ: Final[int] = 1_000_000_000


@dataclass(frozen=True)
class FeatureRecord:
    """Features of one analysis window.

    Attributes:
        mean: Arithmetic mean of the window, in ``g``.
        std: Population standard deviation (``ddof=0``), in ``g``.
        rms: Root mean square ``sqrt(mean(x**2))``, in ``g``. Unlike ``std`` it
            keeps the DC level, which is what makes a step fault visible.
        ptp: Peak-to-peak amplitude ``max(x) - min(x)``, in ``g``.
        kurtosis_excess: Fisher excess kurtosis; ``0.0`` for Gaussian noise and
            for any constant window.
        crest_factor: ``max(|x|) / rms``; ``0.0`` when ``rms == 0``.
        dominant_freq_hz: Frequency of the strongest non-DC ``rfft`` bin, in Hz;
            ``0.0`` for windows shorter than two samples or without variation.
        zero_crossing_rate: Fraction of consecutive sample pairs that cross the
            window mean, in ``[0, 1]``; the mean is removed first so a DC offset
            does not suppress the count.
        sample_count: Number of samples in the window.
        sample_rate_hz: Sampling rate the window was captured at, in Hz.
        window_start_seq: Absolute ``seq`` of the first sample of the window, or
            ``None`` when the caller did not supply it. Alarm windows need it,
            so the consumer must pass it.
    """

    mean: float
    std: float
    rms: float
    ptp: float
    kurtosis_excess: float
    crest_factor: float
    dominant_freq_hz: float
    zero_crossing_rate: float
    sample_count: int
    sample_rate_hz: int
    window_start_seq: int | None = None

    def feature_value(self, name: str) -> float:
        """Return one feature by its contract name.

        Args:
            name: One of :data:`FEATURE_NAMES`.

        Returns:
            The feature value as ``float``.

        Raises:
            KeyError: if ``name`` is not a feature of this record. Callers that
                score health translate this into a configuration error, because
                a rule naming an unknown feature can never be satisfied.
        """
        if name not in FEATURE_NAMES:
            raise KeyError(name)
        return float(getattr(self, name))

    def as_mapping(self) -> dict[str, float]:
        """Return the eight numeric features keyed by name, for storage.

        Returns:
            A new ``dict``; mutating it does not affect the record.
        """
        return {name: float(getattr(self, name)) for name in FEATURE_NAMES}


def extract_features(
    vibration: NDArray[np.float64],
    sample_rate_hz: int,
    *,
    window_start_seq: int | None = None,
) -> FeatureRecord:
    """Extract the section 8.2 feature vector from one window of vibration.

    Args:
        vibration: One-dimensional window of vibration samples in ``g``. The
            array is not modified. Non-finite values are rejected instead of
            being silently replaced, because a silent replacement would hide an
            upstream corruption that the shard contract already forbids.
        sample_rate_hz: Sampling rate of ``vibration`` in Hz.
        window_start_seq: Absolute ``seq`` of the first sample, recorded so that
            a later alarm can name the window. Optional only for callers that
            analyse a window outside a run.

    Returns:
        The :class:`FeatureRecord` of the window.

    Raises:
        ValueError: if ``vibration`` is not a non-empty one-dimensional finite
            **real** array, or its dtype is outside :data:`REAL_SAMPLE_DTYPES`,
            or it carries integers beyond :data:`MAX_EXACT_INTEGER`, or
            ``sample_rate_hz`` is not an integer in ``1..MAX_SAMPLE_RATE_HZ``, or
            ``window_start_seq`` is negative, or a feature of a finite window
            cannot be represented as a finite ``float64`` (see
            :func:`_restore_scale`).
    """
    samples = _as_signal(vibration)
    rate = _as_sample_rate(sample_rate_hz)
    start = _as_window_start(window_start_seq)

    count = int(samples.size)
    scaled, factor = _rescale(samples)
    centered, variance_scaled, rms_scaled, peak_scaled = _scaled_moments(scaled)
    restored = _restore_scale(
        factor=factor,
        mean=float(scaled.mean()),
        variance=variance_scaled,
        rms=rms_scaled,
        ptp=float(scaled.max() - scaled.min()),
    )

    return FeatureRecord(
        mean=restored.mean,
        std=restored.std,
        rms=restored.rms,
        ptp=restored.ptp,
        kurtosis_excess=_excess_kurtosis(centered, variance_scaled),
        crest_factor=peak_scaled / rms_scaled if rms_scaled > 0.0 else 0.0,
        dominant_freq_hz=_dominant_frequency(centered, rate),
        zero_crossing_rate=_zero_crossing_rate(centered, count),
        sample_count=count,
        sample_rate_hz=rate,
        window_start_seq=start,
    )


def _rescale(samples: NDArray[np.float64]) -> tuple[NDArray[np.float64], float]:
    """Divide the window by a power of two and return it with that factor.

    Dividing by ``2**k`` is exact for every finite ``float64`` input except
    where a sample is pushed into the subnormal range, so ordinary windows keep
    their numbers in practice. The factor is chosen so that the largest rescaled
    magnitude lies in ``(1, 2]``: second moments then stay below ``4``, and a
    mean-centred value is bounded by ``4`` in absolute value, so ``centered**4``
    stays below ``256`` -- a coarse but safe bound, which is what keeps
    large-but-finite samples from overflowing the moment computation.

    Args:
        samples: Validated finite, real one-dimensional window.

    Returns:
        A pair ``(scaled, factor)`` with ``samples == scaled * factor`` to
        within the exactness of the division. An all-zero window is returned
        untouched with a factor of ``1.0``.
    """
    peak = float(np.max(np.abs(samples)))
    if peak == 0.0:
        return samples, 1.0
    # ``frexp`` splits ``peak`` into ``m * 2**exponent`` with ``0.5 <= m < 1``,
    # so ``2**(exponent - 1) <= peak < 2**exponent`` and ``ldexp`` cannot
    # overflow for a finite ``peak``.
    factor = math.ldexp(1.0, math.frexp(peak)[1] - 1)
    return samples / factor, factor


def _scaled_moments(
    scaled: NDArray[np.float64],
) -> tuple[NDArray[np.float64], float, float, float]:
    """Return the mean-centred window and the moments needed by the record.

    Args:
        scaled: Window after :func:`_rescale`, so every magnitude is ``<= 2``.

    Returns:
        ``(centered, variance, rms, peak)`` where ``centered`` has its mean
        removed, ``variance`` is the population variance, ``rms`` the root mean
        square and ``peak`` the largest absolute sample of the rescaled window.
    """
    centered = scaled - float(scaled.mean())
    return (
        centered,
        float(np.mean(centered**2)),
        float(np.sqrt(np.mean(scaled**2))),
        float(np.max(np.abs(scaled))),
    )


class _Moments(NamedTuple):
    """Shape features of one window, restored to ``g``."""

    mean: float
    std: float
    rms: float
    ptp: float


def _restore_scale(
    *,
    factor: float,
    mean: float,
    variance: float,
    rms: float,
    ptp: float,
) -> _Moments:
    """Scale the rescaled moments back to ``g`` and refuse non-finite results.

    Args:
        factor: Power of two the window was divided by.
        mean: Mean of the rescaled window.
        variance: Population variance of the rescaled window.
        rms: Root mean square of the rescaled window.
        ptp: Peak-to-peak amplitude of the rescaled window.

    Returns:
        The ``mean``, ``std``, ``rms`` and ``ptp`` of the window in ``g``.

    Raises:
        ValueError: if a scaled-back feature is ``inf``. The window is finite but
            its spread exceeds the ``float64`` range (for example +/-``1.7e308``
            gives a peak-to-peak amplitude of ``3.4e308``); reporting ``inf``
            would let the score and the alarm state machine consume a number
            that is not a measurement.
    """
    restored = {
        "mean": mean * factor,
        "std": math.sqrt(variance) * factor,
        "rms": rms * factor,
        "ptp": ptp * factor,
    }
    overflowed = [name for name, value in restored.items() if not math.isfinite(value)]
    if overflowed:
        raise ValueError(
            "vibration is finite but its "
            + "/".join(overflowed)
            + " exceeds the float64 range even after rescaling by "
            + f"{factor:.3e}; rescale the window before extracting features"
        )
    return _Moments(
        mean=restored["mean"],
        std=restored["std"],
        rms=restored["rms"],
        ptp=restored["ptp"],
    )


def _as_signal(vibration: object) -> NDArray[np.float64]:
    """Validate the raw window and return it as ``float64``.

    Args:
        vibration: Candidate window.

    Returns:
        A one-dimensional ``float64`` array holding the window.

    Raises:
        ValueError: if it is not a non-empty one-dimensional finite real array
            whose dtype is in :data:`REAL_SAMPLE_DTYPES` and whose integer
            values fit :data:`MAX_EXACT_INTEGER`.
    """
    if not isinstance(vibration, np.ndarray):
        raise ValueError(
            f"vibration must be a numpy array, got {type(vibration).__name__}"
        )
    if vibration.ndim != 1:
        raise ValueError(
            f"vibration must be one-dimensional, got ndim={vibration.ndim}"
        )
    if vibration.size == 0:
        raise ValueError("vibration window must not be empty")
    if np.iscomplexobj(vibration):
        # ``astype(float64)`` would only warn and drop the imaginary part, so the
        # record would describe a signal nobody passed in.
        raise ValueError(f"vibration must be real-valued, got dtype {vibration.dtype}")
    if vibration.dtype not in REAL_SAMPLE_DTYPES:
        # Named whitelist, not ``np.issubdtype(dtype, np.number)``: NumPy counts
        # ``timedelta64`` as a number, so the generic test would read a window of
        # time deltas as a vibration and report its day counts as ``g``.
        raise ValueError(
            "vibration must be a real numeric dtype among "
            + "/".join(str(dtype) for dtype in REAL_SAMPLE_DTYPES)
            + f", got dtype {vibration.dtype}"
        )
    if vibration.dtype.kind in "iu":
        _require_exact_integers(vibration)
    samples = np.asarray(vibration, dtype=np.float64)
    if not bool(np.all(np.isfinite(samples))):
        index = int(np.argmax(~np.isfinite(samples)))
        raise ValueError(f"vibration is not finite at local index {index}")
    return samples


def _require_exact_integers(vibration: NDArray[np.integer]) -> None:
    """Reject integer windows that ``float64`` cannot hold exactly.

    ``float64`` represents every integer up to ``2**53`` exactly, and above that
    only some: the spacing doubles, so ``2**53 + 1`` has no ``float64`` value of
    its own while ``2**53 + 2`` does. ``[2**64 - 1, 2**64 - 2]`` collapses to one
    ``float64``, so ``std`` and ``ptp`` come back as ``0.0`` for a window that
    really changed; losing that difference before the moments are formed is not
    something the stable moment code can undo.

    The common path costs one comparison: every value inside
    ``|value| <= MAX_EXACT_INTEGER`` is exact by construction. Only a window that
    reaches past that bound is checked value by value, which is why a value such
    as ``2**53 + 2`` is still accepted instead of being refused by a blanket
    range rule.

    Args:
        vibration: One-dimensional integer window.

    Raises:
        ValueError: if any sample has no exact ``float64`` representation.
    """
    smallest = int(vibration.min())
    largest = int(vibration.max())
    if max(abs(smallest), abs(largest)) <= MAX_EXACT_INTEGER:
        return
    # ``tolist`` gives Python ints and ``int(float(v))`` is exact, so this
    # round-trip is the definition of "representable" rather than an estimate.
    # The loop only runs for windows past 2**53, which ordinary vibration is not.
    for value in vibration.tolist():
        if int(float(value)) != value:
            raise ValueError(
                f"vibration integers must be exactly representable as float64; "
                f"{value} of dtype {vibration.dtype} is not (every |value| <= "
                f"{MAX_EXACT_INTEGER} is, above that only multiples of the "
                "widened spacing are); rescale the window before extracting "
                "features"
            )


def _as_sample_rate(sample_rate_hz: object) -> int:
    """Validate ``sample_rate_hz`` and return it as ``int``.

    Args:
        sample_rate_hz: Candidate sampling rate.

    Returns:
        The rate as ``int``.

    Raises:
        ValueError: if it is not an integer, or not within the contract range.
    """
    if isinstance(sample_rate_hz, bool) or not isinstance(
        sample_rate_hz, (int, np.integer)
    ):
        raise ValueError(
            f"sample_rate_hz must be an integer, got {type(sample_rate_hz).__name__}"
        )
    rate = int(sample_rate_hz)
    if rate <= 0:
        raise ValueError(f"sample_rate_hz must be positive, got {rate}")
    if rate > MAX_SAMPLE_RATE_HZ:
        raise ValueError(
            f"sample_rate_hz must not exceed {MAX_SAMPLE_RATE_HZ}, got {rate}"
        )
    return rate


def _as_window_start(window_start_seq: object) -> int | None:
    """Validate the optional window position.

    Args:
        window_start_seq: Candidate absolute ``seq``.

    Returns:
        ``None`` or a non-negative ``int``.

    Raises:
        ValueError: if the value is not ``None`` or a non-negative integer.
    """
    if window_start_seq is None:
        return None
    if isinstance(window_start_seq, bool) or not isinstance(
        window_start_seq, (int, np.integer)
    ):
        raise ValueError(
            "window_start_seq must be an integer or None, "
            f"got {type(window_start_seq).__name__}"
        )
    start = int(window_start_seq)
    if start < 0:
        raise ValueError(f"window_start_seq must be non-negative, got {start}")
    return start


def _excess_kurtosis(centered: NDArray[np.float64], variance: float) -> float:
    """Return the Fisher excess kurtosis of an already centered window.

    Args:
        centered: Window with its mean removed.
        variance: Population variance of the window.

    Returns:
        ``m4 / m2**2 - 3``, or ``0.0`` when the variance is not positive (a
        constant window has no shape to describe).
    """
    if variance <= 0.0:
        return 0.0
    fourth_moment = float(np.mean(centered**4))
    return fourth_moment / (variance * variance) - 3.0


def _dominant_frequency(centered: NDArray[np.float64], sample_rate_hz: int) -> float:
    """Return the strongest non-DC frequency of an already centered window.

    Args:
        centered: Window with its mean removed, so bin 0 is zero and is skipped.
        sample_rate_hz: Sampling rate in Hz.

    Returns:
        Bin centre ``k * rate / n`` in Hz, or ``0.0`` when the window is shorter
        than two samples, has no variation, or its spectrum carries no energy.
    """
    count = int(centered.size)
    if count < 2:
        return 0.0
    if not bool(np.any(centered)):
        return 0.0

    spectrum = np.abs(np.fft.rfft(centered)) / count
    # One-sided amplitude: interior bins carry half the power in the negative
    # half of the spectrum, so they double; DC and Nyquist do not.
    if count % 2 == 0:
        spectrum[1:-1] *= 2.0
    else:
        spectrum[1:] *= 2.0

    searchable = spectrum[1:]
    if searchable.size == 0:
        return 0.0
    peak_bin = int(np.argmax(searchable)) + 1
    if spectrum[peak_bin] <= 0.0:
        return 0.0
    return peak_bin * sample_rate_hz / count


def _zero_crossing_rate(centered: NDArray[np.float64], count: int) -> float:
    """Return the share of sample pairs that cross the window mean.

    Args:
        centered: Window with its mean removed.
        count: Number of samples in the window.

    Returns:
        ``crossings / (count - 1)`` in ``[0, 1]``, or ``0.0`` for one sample.
    """
    if count < 2:
        return 0.0
    positive = centered >= 0.0
    crossings = int(np.count_nonzero(positive[1:] != positive[:-1]))
    return crossings / (count - 1)
