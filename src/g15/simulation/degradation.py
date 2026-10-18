"""Deterministic degradation trends for the G15 simulator.

This module owns the *explainable* half of the model frozen in
``docs/interface.md`` section 7.1::

    vibration(i)   = v0 + kv * i + noise_v(i)
    temperature(i) = t0 + kt * i + noise_t(i)

Only the deterministic term ``v0 + k * i`` lives here.  The stochastic term is
produced by :mod:`g15.simulation.noise`.

Units and indexing
------------------
* ``i`` is the **absolute** sampling index of the run (the ``seq`` value of the
  frozen shard contract), never a chunk-local offset.  A chunk evaluated on its
  own is therefore bit-identical to the same range inside a one-shot run.
* ``vibration`` is expressed in ``g`` and ``temperature`` in ``degC``; the unit
  string is carried along in :class:`SignalParameters` for reporting, and is
  never used in arithmetic.

Numeric policy
--------------
The trend is **not clamped**.  Silently clipping a diverging trend would hide a
bad configuration behind plausible-looking data, so the engine instead validates
the inputs here and rejects non-finite results after the sum is formed (see
:func:`g15.simulation.engine.simulate_chunk`).  Negative vibration values are
physically meaningful (oscillation about the baseline) and are allowed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

__all__ = ["SignalParameters", "linear_trend"]

#: Unit strings accepted for the vibration channel.
VIBRATION_UNIT: Final[str] = "g"

#: Unit strings accepted for the temperature channel.
TEMPERATURE_UNIT: Final[str] = "degC"


def _finite_float(value: object, path: str) -> float:
    """Return ``value`` as a finite float.

    Args:
        value: Candidate value, typically read from the scenario configuration.
        path: Dotted field path used in the error message.

    Returns:
        The value as a Python float.

    Raises:
        ValueError: if ``value`` is not a finite real number.
    """
    if isinstance(value, bool):
        raise ValueError(f"{path} must be a finite number, got {value!r}")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} must be a finite number, got {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"{path} must be finite, got {value!r}")
    return result


@dataclass(frozen=True)
class SignalParameters:
    """Validated parameters of one signal channel.

    Attributes:
        baseline: Value at sample index ``0``; ``v0`` in ``g`` for vibration and
            ``t0`` in ``degC`` for temperature.
        degradation_rate_per_sample: Slope ``k`` per sample index, in the unit of
            the channel per sample.  Must be finite and non-negative: the
            simulator models wear (monotone drift), not self-repair.
        noise_std: Standard deviation ``sigma`` of the additive Gaussian noise,
            in the unit of the channel.  Must be finite and non-negative.
        unit: Physical unit of the channel, for reporting only.
    """

    baseline: float
    degradation_rate_per_sample: float
    noise_std: float
    unit: str = ""

    @classmethod
    def from_values(
        cls,
        baseline: object,
        degradation_rate_per_sample: object,
        noise_std: object,
        unit: object = "",
        *,
        path: str = "signals",
    ) -> SignalParameters:
        """Build a validated instance from loosely typed configuration values.

        Args:
            baseline: Baseline level of the channel.
            degradation_rate_per_sample: Slope per sample index.
            noise_std: Standard deviation of the additive noise.
            unit: Physical unit string; defaults to an empty string.
            path: Dotted field path used in error messages.

        Returns:
            A frozen :class:`SignalParameters` instance.

        Raises:
            ValueError: if a rate or deviation is negative, or any numeric field
                is not finite.
        """
        base = _finite_float(baseline, f"{path}.baseline")
        rate_path = f"{path}.degradation_rate_per_sample"
        rate = _finite_float(degradation_rate_per_sample, rate_path)
        if rate < 0.0:
            raise ValueError(f"{rate_path} must be non-negative, got {rate}")
        deviation = _finite_float(noise_std, f"{path}.noise_std")
        if deviation < 0.0:
            raise ValueError(f"{path}.noise_std must be non-negative, got {deviation}")
        if unit is None:
            unit = ""
        if not isinstance(unit, str):
            raise ValueError(f"{path}.unit must be a string, got {unit!r}")
        return cls(
            baseline=base,
            degradation_rate_per_sample=rate,
            noise_std=deviation,
            unit=unit,
        )


def linear_trend(
    start_seq: int,
    count: int,
    baseline: float,
    degradation_rate_per_sample: float,
) -> NDArray[np.float64]:
    """Evaluate ``baseline + rate * seq`` for ``count`` samples from ``start_seq``.

    The computation is a single vectorised NumPy expression; no Python-level
    per-sample loop is used, as required by ``members/B1/AGENT.md``.

    Args:
        start_seq: Absolute index of the first returned sample; must be
            non-negative.
        count: Number of samples; must be non-negative.  ``0`` yields an empty
            array.
        baseline: Value at sample index ``0``.
        degradation_rate_per_sample: Slope per sample index.

    Returns:
        A ``float64`` array of shape ``(count,)``.

    Raises:
        ValueError: if ``start_seq`` or ``count`` is negative.
    """
    if start_seq < 0:
        raise ValueError(f"start_seq must be non-negative, got {start_seq}")
    if count < 0:
        raise ValueError(f"count must be non-negative, got {count}")
    seq = np.arange(start_seq, start_seq + count, dtype=np.int64)
    # Overflow and invalid results are deliberately allowed to occur here and are
    # detected by the caller (engine._check_finite), which reports the offending
    # channel and absolute sample index.  Suppressing the NumPy warning keeps the
    # failure a single explicit error instead of a warning plus a later raise.
    with np.errstate(over="ignore", invalid="ignore"):
        trend = seq.astype(np.float64)
        trend *= float(degradation_rate_per_sample)
        trend += float(baseline)
    return trend
