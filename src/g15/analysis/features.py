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
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "FEATURES_VERSION",
    "FEATURE_NAMES",
    "FeatureRecord",
    "extract_features",
]

#: Version of the feature definitions in this module.
#:
#: Bump it when a definition changes in a way that moves numbers for the same
#: input (for example switching ``std`` to ``ddof=1``). Reports copy it so that a
#: stored feature vector can be traced back to the code that produced it.
FEATURES_VERSION: Final[str] = "1.0"

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
            array, or ``sample_rate_hz`` is not an integer in
            ``1..MAX_SAMPLE_RATE_HZ``, or ``window_start_seq`` is negative.
    """
    samples = _as_signal(vibration)
    rate = _as_sample_rate(sample_rate_hz)
    start = _as_window_start(window_start_seq)

    count = int(samples.size)
    mean = float(samples.mean())
    centered = samples - mean
    variance = float(np.mean(centered**2))
    rms = float(np.sqrt(np.mean(samples**2)))
    peak = float(np.max(np.abs(samples)))

    return FeatureRecord(
        mean=mean,
        std=math.sqrt(variance),
        rms=rms,
        ptp=float(samples.max() - samples.min()),
        kurtosis_excess=_excess_kurtosis(centered, variance),
        crest_factor=peak / rms if rms > 0.0 else 0.0,
        dominant_freq_hz=_dominant_frequency(centered, rate),
        zero_crossing_rate=_zero_crossing_rate(centered, count),
        sample_count=count,
        sample_rate_hz=rate,
        window_start_seq=start,
    )


def _as_signal(vibration: object) -> NDArray[np.float64]:
    """Validate the raw window and return it as ``float64``.

    Args:
        vibration: Candidate window.

    Returns:
        A one-dimensional ``float64`` array holding the window.

    Raises:
        ValueError: if it is not a non-empty one-dimensional finite array.
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
    if not np.issubdtype(vibration.dtype, np.number):
        raise ValueError(f"vibration must be numeric, got dtype {vibration.dtype}")
    samples = np.asarray(vibration, dtype=np.float64)
    if not bool(np.all(np.isfinite(samples))):
        index = int(np.argmax(~np.isfinite(samples)))
        raise ValueError(f"vibration is not finite at local index {index}")
    return samples


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
