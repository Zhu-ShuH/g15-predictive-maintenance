"""Chunk-addressable simulation engine for the G15 predictive-maintenance sandbox.

The engine turns a validated scenario configuration into the four equal-length
arrays frozen by ``docs/interface.md`` section 5.1::

    seq: int64, timestamp_ns: int64, vibration: float64 (g), temperature: float64 (degC)

Determinism strategy
--------------------
``docs/interface.md`` section 4 requires ``numpy.random.default_rng(seed)`` and
stable sub-streams; ``members/B1/AGENT.md`` additionally requires a stated
policy for *different chunk sizes and retries*.  A single sequential generator
cannot satisfy the second requirement: replaying chunk 7 after a crash, or
splitting the run into different shard sizes, would shift every later draw.

This engine therefore addresses noise by ``(seed, stream, absolute index)``
instead of by draw order (see :mod:`g15.simulation.noise`).  Consequences:

* ``simulate_chunk(cfg, 0, 102400)`` followed by ``simulate_chunk(cfg, 102400, 102400)``
  is bit-identical to a single ``simulate_chunk(cfg, 0, 204800)``.
* A chunk can be regenerated in isolation, in any order, any number of times.
* Changing the vibration noise model cannot move a single temperature sample.

The ``rng`` parameter of :func:`simulate_chunk` is part of the frozen signature
in ``docs/interface.md`` section 6.  It is accepted and type-checked, but it
deliberately does **not** feed the signal noise: doing so would reintroduce
draw-order dependence and break the guarantees above.  Stochastic fault
injection (``simulation/faults.py``, owned by C1) receives the same treatment.

Numeric policy
--------------
Trends are never clamped and never wrapped.  Inputs are validated before any
arithmetic, and the finished arrays are checked for non-finite values; a
diverging or overflowing configuration raises :class:`ValueError` naming the
offending channel and sample index instead of silently emitting plausible data.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from g15.common.types import SimulationChunk
from g15.simulation.degradation import SignalParameters, linear_trend
from g15.simulation.noise import STREAM_TEMPERATURE, STREAM_VIBRATION, gaussian_noise

__all__ = [
    "NANOSECONDS_PER_SECOND",
    "SimulationChunk",
    "ScenarioParameters",
    "load_scenario_parameters",
    "simulate_chunk",
    "timestamp_ns_for",
]

#: Number of nanoseconds in one second, used for the exact UTC time base.
NANOSECONDS_PER_SECOND: int = 1_000_000_000

#: Inclusive bounds of the ``int64`` time base used by ``timestamp_ns``.
_INT64_MIN: int = -(2**63)
_INT64_MAX: int = 2**63 - 1


def _offset_ns(sample_index: int, sample_rate_hz: int) -> int:
    """Return the exact sub-second-rounded offset of one sample, in Python ints.

    This mirrors the vectorised expression in :func:`timestamp_ns_for`
    (``(remainder * 1e9 + rate // 2) // rate`` plus whole seconds) without any
    fixed-width arithmetic, so it can be used to test the bounds before NumPy
    ever sees the values.  Because ``sample_index = q * rate + r`` with
    ``0 <= r < rate``, the whole-second part is an exact multiple of ``rate``
    and the single quotient below is identical to the split form.

    Args:
        sample_index: Absolute sampling index; must be non-negative.
        sample_rate_hz: Sampling frequency in hertz; must be positive.

    Returns:
        Nanoseconds since the start instant, rounded half up.
    """
    return (
        sample_index * NANOSECONDS_PER_SECOND + sample_rate_hz // 2
    ) // sample_rate_hz


#: Unix epoch in UTC, the origin of ``timestamp_ns``.
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


class _Absent:
    """Sentinel type for "field not present", distinct from a stored ``None``."""

    # A sentinel carries no behaviour; its identity is the whole contract.
    # pylint: disable=too-few-public-methods

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


#: Sentinel returned by :func:`_lookup` when a field is not present.
_ABSENT = _Absent()


def _lookup(source: object, name: str) -> Any:
    """Return ``source[name]`` or ``source.name``, or :data:`_ABSENT`."""
    if source is None:
        return _ABSENT
    if isinstance(source, Mapping):
        return source[name] if name in source else _ABSENT
    return getattr(source, name, _ABSENT)


def _field(source: object, name: str, path: str, default: Any = _ABSENT) -> Any:
    """Read ``name`` from a mapping or an object attribute.

    ``ScenarioConfig`` is owned by C3 and its concrete shape is not fixed yet
    (Issue #4 is still open), so the engine reads it structurally.  Both a
    ``Mapping`` and a plain object with attributes are accepted.

    Args:
        source: Mapping or object to read from.
        name: Field name.
        path: Dotted path used in error messages.
        default: Value returned when the field is absent; when omitted, an
            absent field is an error.

    Returns:
        The field value, or ``default``.

    Raises:
        ValueError: if the field is absent and no default was supplied.
    """
    value = _lookup(source, name)
    if value is not _ABSENT:
        return value
    if default is not _ABSENT:
        return default
    raise ValueError(
        f"{path} is missing: the scenario configuration has no field {name!r}"
    )


def _nested(source: object, name: str) -> Any:
    """Read a nested block, tolerating a flat layout.

    Accepts ``config.scenario`` / ``config["scenario"]`` when present, and falls
    back to ``config`` itself so that a flat configuration object also works.

    Args:
        source: Scenario configuration.
        name: Name of the nested block, e.g. ``"scenario"`` or ``"signals"``.

    Returns:
        The nested block, or the source object itself when the block is absent
        but the source already carries the fields.
    """
    block = _lookup(source, name)
    return source if block is _ABSENT else block


def _as_int(value: object, path: str) -> int:
    """Return ``value`` as a Python int.

    Raises:
        ValueError: if ``value`` is not integral and finite.
    """
    if isinstance(value, bool):
        raise ValueError(f"{path} must be an integer, got {value!r}")
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if not math.isfinite(number) or number != int(number):
            raise ValueError(f"{path} must be an integer, got {value!r}")
        return int(number)
    raise ValueError(f"{path} must be an integer, got {value!r}")


def _parse_start_time(value: object, path: str) -> int:
    """Return the scenario start instant as UTC Unix nanoseconds.

    Args:
        value: ``datetime``, ISO-8601 string, or integer nanosecond count.
        path: Dotted path used in error messages.

    Returns:
        Nanoseconds since the Unix epoch, computed with integer arithmetic so
        that no floating-point rounding is introduced.

    Raises:
        ValueError: if the value cannot be interpreted as an instant.
    """
    if isinstance(value, _dt.datetime):
        moment = value
    elif isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{path} must not be empty")
        try:
            moment = _dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"{path} is not a valid ISO-8601 timestamp: {value!r}"
            ) from exc
    else:
        raise ValueError(
            f"{path} must be an ISO-8601 string or datetime, got {value!r}"
        )

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    delta = moment.astimezone(_dt.timezone.utc) - _EPOCH
    return (
        delta.days * 86400 + delta.seconds
    ) * NANOSECONDS_PER_SECOND + delta.microseconds * 1000


@dataclass(frozen=True)
class ScenarioParameters:
    """The subset of the scenario configuration the simulation kernel needs.

    Attributes:
        seed: Seed of the run, shared by every sub-stream.
        sample_rate_hz: Sampling frequency in hertz; must be positive.
        start_time_utc_ns: Run start instant as UTC Unix nanoseconds.
        total_points: Declared length of the run in samples; must be positive.
        vibration: Validated vibration channel parameters (unit ``g``).
        temperature: Validated temperature channel parameters (unit ``degC``).
        device_id: Device identifier, carried through for reporting only.
    """

    seed: int
    sample_rate_hz: int
    start_time_utc_ns: int
    total_points: int
    vibration: SignalParameters
    temperature: SignalParameters
    device_id: str = ""


def _signal_parameters(signals: object, name: str) -> SignalParameters:
    """Build :class:`SignalParameters` for one channel of the configuration."""
    block = _field(signals, name, f"signals.{name}")
    return SignalParameters.from_values(
        _field(block, "baseline", f"signals.{name}.baseline"),
        _field(
            block,
            "degradation_rate_per_sample",
            f"signals.{name}.degradation_rate_per_sample",
        ),
        _field(block, "noise_std", f"signals.{name}.noise_std"),
        _field(block, "unit", f"signals.{name}.unit", ""),
        path=f"signals.{name}",
    )


def load_scenario_parameters(config: object) -> ScenarioParameters:
    """Extract and validate the simulation-relevant fields of a scenario config.

    The function is read-only and never mutates ``config``.  It accepts the
    nested layout of ``docs/interface.md`` section 4 (``scenario`` and
    ``signals`` blocks) as well as a flat object exposing the same field names.

    Args:
        config: A ``ScenarioConfig`` from :mod:`g15.config` (mapping or object).

    Returns:
        A frozen :class:`ScenarioParameters` instance.

    Raises:
        ValueError: if a required field is missing, a rate or deviation is
            negative, ``total_points``/``sample_rate_hz`` is not positive, or the
            start timestamp cannot be parsed.
    """
    scenario = _nested(config, "scenario")
    signals = _nested(config, "signals")

    seed = _as_int(_field(scenario, "seed", "scenario.seed"), "scenario.seed")
    if seed < 0:
        raise ValueError(f"scenario.seed must be non-negative, got {seed}")

    sample_rate_hz = _as_int(
        _field(scenario, "sample_rate_hz", "scenario.sample_rate_hz"),
        "scenario.sample_rate_hz",
    )
    if sample_rate_hz <= 0:
        raise ValueError(
            f"scenario.sample_rate_hz must be positive, got {sample_rate_hz}"
        )

    total_points = _as_int(
        _field(scenario, "total_points", "scenario.total_points"),
        "scenario.total_points",
    )
    if total_points <= 0:
        raise ValueError(f"scenario.total_points must be positive, got {total_points}")

    start_ns = _parse_start_time(
        _field(scenario, "start_time_utc", "scenario.start_time_utc"),
        "scenario.start_time_utc",
    )

    device_id = _field(scenario, "device_id", "scenario.device_id", "")
    if device_id is None:
        device_id = ""

    return ScenarioParameters(
        seed=seed,
        sample_rate_hz=sample_rate_hz,
        start_time_utc_ns=start_ns,
        total_points=total_points,
        vibration=_signal_parameters(signals, "vibration"),
        temperature=_signal_parameters(signals, "temperature"),
        device_id=str(device_id),
    )


def timestamp_ns_for(
    start_seq: int,
    count: int,
    start_time_utc_ns: int,
    sample_rate_hz: int,
) -> NDArray[np.int64]:
    """Return UTC Unix nanoseconds for ``count`` samples starting at ``start_seq``.

    The clock is derived from the absolute sampling index, never by accumulating
    a floating-point period (``docs/interface.md`` section 4).  Integer
    arithmetic is used throughout: ``seq`` is split into whole seconds and a
    remainder, so the result stays exact and never overflows for realistic runs.

    Args:
        start_seq: Absolute index of the first sample; must be non-negative.
        count: Number of samples; must be non-negative.
        start_time_utc_ns: Run start instant as UTC Unix nanoseconds.
        sample_rate_hz: Sampling frequency in hertz; must be positive.

    Returns:
        An ``int64`` array of shape ``(count,)``, strictly increasing because
        ``sample_rate_hz`` is positive.

    Raises:
        ValueError: for a negative index or count, a non-positive rate, or a
            range whose timestamps would not fit in ``int64`` Unix nanoseconds.
    """
    if start_seq < 0:
        raise ValueError(f"start_seq must be non-negative, got {start_seq}")
    if count < 0:
        raise ValueError(f"count must be non-negative, got {count}")
    if sample_rate_hz <= 0:
        raise ValueError(f"sample_rate_hz must be positive, got {sample_rate_hz}")
    if count == 0:
        return np.empty(0, dtype=np.int64)

    # Both bounds are checked with Python integers before any NumPy arithmetic:
    # int64 overflow in the vectorised path wraps silently and would return a
    # plausible-but-wrong clock instead of an error.  The check uses the very
    # same half-up rounding as the vectorised path (_offset_ns), and covers the
    # first sample as well as the last: the start instant alone can already sit
    # outside int64, and a floor-rounded estimate would let the final sample
    # overflow by a few nanoseconds on rates that do not divide 1e9 evenly.
    if not _INT64_MIN <= start_time_utc_ns <= _INT64_MAX:
        raise ValueError(
            f"start_time_utc_ns={start_time_utc_ns} is outside the int64 "
            "nanosecond range"
        )
    last_seq = start_seq + count - 1
    if last_seq > _INT64_MAX:
        # ``seq`` is int64 by contract, so an index beyond it cannot be
        # represented at all - even when the resulting timestamps would fit.
        raise ValueError(
            f"seq index {last_seq} does not fit in int64; the last sample of "
            f"seq [{start_seq}, {start_seq + count}) is out of range"
        )
    first_ns = _offset_ns(start_seq, sample_rate_hz) + start_time_utc_ns
    last_ns = _offset_ns(last_seq, sample_rate_hz) + start_time_utc_ns
    if last_ns > _INT64_MAX or first_ns < _INT64_MIN or first_ns > _INT64_MAX:
        raise ValueError(
            f"timestamps for seq [{start_seq}, {start_seq + count}) do not fit "
            f"in int64 nanoseconds (first value {first_ns}, last value {last_ns})"
        )

    seq = np.arange(start_seq, start_seq + count, dtype=np.int64)
    whole_seconds = seq // sample_rate_hz
    remainder = seq % sample_rate_hz
    # Round the sub-second remainder to the nearest nanosecond (half up).
    offset = remainder * NANOSECONDS_PER_SECOND
    offset += sample_rate_hz // 2
    offset //= sample_rate_hz
    offset += whole_seconds * NANOSECONDS_PER_SECOND
    return offset + np.int64(start_time_utc_ns)


def _check_finite(array: NDArray[np.float64], name: str, start_seq: int) -> None:
    """Raise if ``array`` holds a non-finite value, naming the absolute index.

    Args:
        array: Signal array to inspect.
        name: Channel name used in the error message.
        start_seq: Absolute index of ``array[0]``.

    Raises:
        ValueError: if any element is NaN or infinite.
    """
    finite = np.isfinite(array)
    if bool(np.all(finite)):
        return
    local = int(np.argmax(~finite))
    absolute = start_seq + local
    value = array[local]
    kind = "NaN" if np.isnan(value) else "Inf"
    raise ValueError(
        f"{name} is not finite at sample index {absolute} ({kind}, value={value!r}); "
        "check baseline, degradation_rate_per_sample and noise_std in the scenario"
    )


def simulate_chunk(
    config: object,
    start_seq: int,
    count: int,
    rng: np.random.Generator | None = None,
) -> SimulationChunk:
    """Simulate one shard of the run, starting at absolute index ``start_seq``.

    Args:
        config: Scenario configuration (``ScenarioConfig`` from :mod:`g15.config`,
            mapping or object).  Read-only.
        start_seq: Absolute sampling index of the first sample; must be
            non-negative and within ``total_points``.
        count: Number of samples to generate; must be non-negative.  ``0``
            yields an empty chunk with correct dtypes, which is what a producer
            needs for a degenerate last shard.
        rng: Accepted for signature compatibility with ``docs/interface.md``
            section 6 and type-checked, but intentionally not used: noise is
            addressed by ``(seed, stream, absolute index)`` so that results do
            not depend on chunk size, chunk order, or retries.  See the module
            docstring for the full rationale.

    Returns:
        A :class:`SimulationChunk` whose four arrays share the requested length.

    Raises:
        TypeError: if ``rng`` is given but is not a NumPy generator.
        ValueError: if an index is out of range, the configuration is invalid,
            or a generated value is not finite.
    """
    if rng is not None and not isinstance(rng, np.random.Generator):
        raise TypeError(
            f"rng must be a numpy.random.Generator, got {type(rng).__name__}"
        )
    if count < 0:
        raise ValueError(f"count must be non-negative, got {count}")
    if start_seq < 0:
        raise ValueError(f"start_seq must be non-negative, got {start_seq}")

    params = load_scenario_parameters(config)
    stop_seq = start_seq + count
    if stop_seq > params.total_points:
        raise ValueError(
            f"chunk [{start_seq}, {stop_seq}) exceeds scenario.total_points="
            f"{params.total_points}"
        )

    vibration = linear_trend(
        start_seq,
        count,
        params.vibration.baseline,
        params.vibration.degradation_rate_per_sample,
    )
    temperature = linear_trend(
        start_seq,
        count,
        params.temperature.baseline,
        params.temperature.degradation_rate_per_sample,
    )

    vibration += gaussian_noise(
        params.seed, STREAM_VIBRATION, start_seq, count, params.vibration.noise_std
    )
    temperature += gaussian_noise(
        params.seed, STREAM_TEMPERATURE, start_seq, count, params.temperature.noise_std
    )

    _check_finite(vibration, "vibration", start_seq)
    _check_finite(temperature, "temperature", start_seq)

    return SimulationChunk(
        seq=np.arange(start_seq, stop_seq, dtype=np.int64),
        timestamp_ns=timestamp_ns_for(
            start_seq, count, params.start_time_utc_ns, params.sample_rate_hz
        ),
        vibration=vibration,
        temperature=temperature,
    )
