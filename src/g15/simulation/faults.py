"""Controlled fault injection (spike / step / burst) for the G15 sandbox.

Issue #6 (owner C1). Contract sources:

* ``docs/interface.md`` section 7.2 -- the three injection types and the
  additive overlap rule.
* ``docs/interface.md`` section 5.3 -- the truth record, readable by the
  validator only (``event_id``, ``device_id``, ``type``, ``start_seq``,
  ``end_seq_exclusive``, ``amplitude``).
* ``docs/interface.md`` section 6 -- the frozen signature
  ``inject_faults(chunk, config, rng) -> (SimulationChunk, list[FaultEvent])``.

Waveforms
---------
Section 7.2 names the three types but fixes no arithmetic, so the expressions
below are *proposals* pending A/B1 sign-off. In every expression ``k`` is the
offset from the event start, ``k = seq - start_seq``, computed from absolute
sample indices, never from a per-chunk local counter.

===========  ==========================================  ============================
type         expression                                  notes
===========  ==========================================  ============================
``spike``    ``A * exp(-k / tau)``, ``tau = D / 4``      peak ``A`` on the first
                                                         sample, decays to
                                                         ``0.018 * A`` at the end
``step``     ``A`` for every sample of ``[s, s + D)``    off outside the interval
``burst``    ``A * sin(2*pi*f*k/sr) * hann(k)``,         starts and ends at exactly
             ``f = sr / 8``, ``hann(k) = 0.5 - 0.5 *    0, so a chunk boundary
             cos(2*pi*k/(D - 1))``                       inside an event adds no
                                                         step discontinuity
===========  ==========================================  ============================

``D`` is ``duration_points``, ``sr`` is ``scenario.sample_rate_hz``, ``A`` is
``amplitude``. Only ``vibration`` is modified: section 7.2 does not authorize
temperature injection, so temperature, ``seq`` and ``timestamp_ns`` are passed
through untouched. A ``burst`` additionally requires ``D >= 3``; shorter
events are rejected because their Hann window would be identically zero, which
would make them undetectable while still counting towards the detection
denominator (A ruling on PR #27). Legal single-point ``spike``/``step``
events are unaffected.

Overlap policy
--------------
Section 7.2 requires a stated policy; this module implements **additive
superposition** and is tested for it. Every event is accumulated into one copy
of the vibration array, so two events covering the same sample contribute the
sum of their perturbations -- later events never overwrite earlier ones.

Determinism and ``rng``
-----------------------
The three waveforms are closed-form functions of ``(event, absolute seq)``:
they consume no random numbers. ``rng`` is therefore accepted (the signature in
section 6 is frozen and shared with :func:`g15.simulation.engine.simulate_chunk`)
and type-checked, but deliberately unused -- exactly the policy B1 documented
for the simulation kernel. This is what makes a chunk boundary invisible to the
waveform: chunk ``[8, 13)`` split as ``[8, 10)`` + ``[10, 13)`` is bit-identical
to the one-shot result.

``mode: stochastic`` needs a random event *schedule* (type / amplitude /
duration distribution), which ``configs/*.yaml`` V1.0 does not define. Until
that contract is settled in an ADR, requesting a non-zero
``stochastic_probability_per_window`` raises :class:`NotImplementedError`
instead of inventing parameters.

Chunk ownership
---------------
``SimulationChunk`` has a unique home in :mod:`g15.common.types` since
Issue #17 (PR #31); :mod:`g15.simulation.engine` re-exports the same object.
This module only needs the four array attributes plus a dataclass ``replace``,
so it does not import the type at runtime and only references it for
annotations.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
    from g15.common.types import SimulationChunk

__all__ = ["FAULT_TYPES", "FaultEvent", "inject_faults"]

#: Injection types frozen by ``docs/interface.md`` section 7.2.
FAULT_TYPES: Final[tuple[str, ...]] = ("spike", "step", "burst")

#: Fault generation modes named by ``configs/*.yaml`` section 4.
_MODES: Final[tuple[str, ...]] = ("manual", "stochastic", "combined")

#: ``spike`` decay constant as a fraction of the event duration.
_SPIKE_DECAY_FRACTION: Final[float] = 4.0

#: ``burst`` oscillation frequency expressed as samples per cycle.
_BURST_SAMPLES_PER_CYCLE: Final[float] = 8.0

#: Shortest ``burst`` the injector accepts.  The Hann window is identically
#: zero for ``D <= 2`` (and ``D == 1`` additionally sits on a carrier zero
#: crossing), so shorter events would be injected as silence while still
#: counting towards the detection denominator.  A ruling on PR #27: reject
#: them instead of guessing a waveform.
_BURST_MIN_DURATION: Final[int] = 3

_ABSENT: Final[object] = object()

_UINT64_MASK: Final[int] = 0xFFFFFFFFFFFFFFFF


def _lookup(source: object, name: str) -> Any:
    """Return ``source[name]`` or ``source.name``, or a sentinel when absent."""
    if source is None:
        return _ABSENT
    if isinstance(source, Mapping):
        return source[name] if name in source else _ABSENT
    return getattr(source, name, _ABSENT)


def _field(source: object, name: str, path: str, default: Any = _ABSENT) -> Any:
    """Read one field from a mapping or an object attribute.

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
    raise ValueError(f"{path} is missing: no field {name!r}")


def _nested(source: object, name: str) -> object:
    """Return the nested block ``name``, tolerating a flat configuration."""
    block = _lookup(source, name)
    return source if block is _ABSENT else block


def _as_int(value: object, path: str) -> int:
    """Return ``value`` as a Python int.

    Raises:
        ValueError: if ``value`` is not a whole, finite number.
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


def _as_finite_float(value: object, path: str) -> float:
    """Return ``value`` as a finite float.

    Raises:
        ValueError: if ``value`` is not a finite real number.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} must be a finite number, got {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{path} must be finite, got {value!r}")
    return number


@dataclass(frozen=True)
class FaultEvent:
    """One injected event, shaped after ``docs/interface.md`` section 5.3.

    The interval is half open, ``[start_seq, end_seq_exclusive)``, in absolute
    sample indices of the run -- never a chunk-local offset. A single event
    keeps the same record in every chunk it touches; the run-level truth file
    is de-duplicated by ``event_id`` in the producer (C4), not here.

    Attributes:
        event_id: Identifier unique inside one run, e.g. ``"F001"``.
        device_id: Device the run belongs to.
        type: One of :data:`FAULT_TYPES`.
        start_seq: Absolute index of the first affected sample.
        end_seq_exclusive: Absolute index one past the last affected sample.
        amplitude: Peak perturbation in ``g`` for ``spike``/``burst``, constant
            offset in ``g`` for ``step``.
    """

    event_id: str
    device_id: str
    type: str
    start_seq: int
    end_seq_exclusive: int
    amplitude: float

    def __post_init__(self) -> None:
        """Validate the record against section 5.3 and section 7.2.

        Raises:
            ValueError: for an empty identifier, an unknown type, a negative
                start, an empty or inverted interval, or a non-finite
                amplitude.
        """
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError(
                f"event_id must be a non-empty string, got {self.event_id!r}"
            )
        if self.type not in FAULT_TYPES:
            raise ValueError(
                f"unsupported fault type {self.type!r}; expected one of {FAULT_TYPES}"
            )
        if self.start_seq < 0:
            raise ValueError(f"start_seq must be non-negative, got {self.start_seq}")
        if self.end_seq_exclusive <= self.start_seq:
            raise ValueError(
                "end_seq_exclusive must be greater than start_seq, got "
                f"[{self.start_seq}, {self.end_seq_exclusive})"
            )
        if not math.isfinite(self.amplitude):
            raise ValueError(f"amplitude must be finite, got {self.amplitude!r}")

    @property
    def duration_points(self) -> int:
        """Number of affected samples, ``end_seq_exclusive - start_seq``."""
        return self.end_seq_exclusive - self.start_seq

    def to_dict(self) -> dict[str, Any]:
        """Return the section 5.3 truth record for ``private_truth/faults.json``.

        The key set is exactly the frozen one; extra fields would change the
        file the validator consumes, so nothing else is exported here.
        """
        return {
            "event_id": self.event_id,
            "device_id": self.device_id,
            "type": self.type,
            "start_seq": self.start_seq,
            "end_seq_exclusive": self.end_seq_exclusive,
            "amplitude": self.amplitude,
        }


def _spike_waveform(offset: np.ndarray, amplitude: float, duration: int) -> np.ndarray:
    """Return ``A * exp(-k / (D / 4))`` for offsets ``k``."""
    tau = max(duration / _SPIKE_DECAY_FRACTION, np.finfo(np.float64).tiny)
    return amplitude * np.exp(-offset / tau)


def _step_waveform(offset: np.ndarray, amplitude: float) -> np.ndarray:
    """Return the constant offset ``A`` for every sample of the interval."""
    return np.full(offset.shape, amplitude, dtype=np.float64)


def _burst_waveform(offset: np.ndarray, amplitude: float, duration: int) -> np.ndarray:
    """Return a windowed oscillation ``A * sin(2*pi*k/8) * hann(k)``.

    The carrier completes ``_BURST_SAMPLES_PER_CYCLE = 8`` samples per period,
    i.e. a physical frequency of ``sample_rate_hz / 8`` Hz (25 Hz at the
    default 100 Hz rate).  The phase is driven by the absolute offset ``k`` in
    samples, so the waveform shape is identical at every sampling rate when
    indexed by ``seq``; only the physical frequency it represents scales with
    ``sample_rate_hz``.  That is the frozen behaviour, not an aliasing defect.

    The window is the Hann shape normalised by ``D - 1``, so the first and the
    last sample of the event are exactly ``0``.  That keeps a chunk boundary
    inside the event free of any artificial jump, and makes the waveform
    identical whether it is generated in one pass or per chunk.

    Callers must guarantee ``duration >= _BURST_MIN_DURATION``: for shorter
    events the window would be identically zero and the carrier's first sample
    sits on a zero crossing, so the "injected" signal would be indistinguishable
    from an untouched one while the truth record still counts towards the
    detection denominator.  :func:`_parse_events` rejects such events outright.
    """
    phase = 2.0 * np.pi * offset / _BURST_SAMPLES_PER_CYCLE
    window = 0.5 - 0.5 * np.cos(2.0 * np.pi * offset / (duration - 1))
    return amplitude * np.sin(phase) * window


def _waveform(
    event: FaultEvent, low: int, high: int, sample_rate_hz: int
) -> np.ndarray:
    """Build the perturbation of ``event`` over absolute ``[low, high)``.

    Args:
        event: Event being injected.
        low: First absolute index of the overlap with the current chunk.
        high: One past the last absolute index of the overlap.
        sample_rate_hz: Sampling frequency, used by ``burst``.

    Returns:
        A ``float64`` array of length ``high - low``.

    Raises:
        ValueError: if a ``burst`` event is requested without a positive
            sampling rate (the phase would be undefined).
    """
    offset = np.arange(low, high, dtype=np.int64) - event.start_seq
    if event.type == "spike":
        return _spike_waveform(offset, event.amplitude, event.duration_points)
    if event.type == "step":
        return _step_waveform(offset, event.amplitude)
    if sample_rate_hz <= 0:
        raise ValueError(
            "burst injection needs scenario.sample_rate_hz > 0, got "
            f"{sample_rate_hz}"
        )
    return _burst_waveform(offset, event.amplitude, event.duration_points)


def _parse_events(
    raw: object, device_id: str, total_points: int | None
) -> list[FaultEvent]:
    """Convert ``fault_generation.events`` into validated :class:`FaultEvent`.

    Args:
        raw: The ``events`` sequence from the configuration; ``None`` and an
            empty sequence both mean "no event".
        device_id: Device identifier copied into every record.
        total_points: Declared run length, when known; used to reject events
            that end past the run.

    Returns:
        The events in configuration order.

    Raises:
        ValueError: for a malformed event, a duplicated ``event_id``, a
            ``burst`` shorter than ``_BURST_MIN_DURATION``, or an interval
            that leaves ``[0, total_points)``.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError(
            "fault_generation.events must be a sequence of events, got "
            f"{type(raw).__name__}"
        )

    events: list[FaultEvent] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        path = f"fault_generation.events[{index}]"
        event_id = str(_field(item, "event_id", f"{path}.event_id")).strip()
        if event_id in seen:
            raise ValueError(
                f"{path}.event_id={event_id!r} is duplicated; section 7.2 needs "
                "one stable identifier per event"
            )
        seen.add(event_id)

        kind = str(_field(item, "type", f"{path}.type")).strip()
        start = _as_int(
            _field(item, "start_seq", f"{path}.start_seq"), f"{path}.start_seq"
        )
        duration = _as_int(
            _field(item, "duration_points", f"{path}.duration_points"),
            f"{path}.duration_points",
        )
        amplitude = _as_finite_float(
            _field(item, "amplitude", f"{path}.amplitude"), f"{path}.amplitude"
        )
        if start < 0:
            raise ValueError(f"{path}.start_seq must be non-negative, got {start}")
        if duration <= 0:
            raise ValueError(f"{path}.duration_points must be positive, got {duration}")
        if kind == "burst" and duration < _BURST_MIN_DURATION:
            raise ValueError(
                f"{path}: burst requires duration_points >= {_BURST_MIN_DURATION}, "
                f"got {duration}. The Hann window is identically zero for shorter "
                "events, so the injected signal would be indistinguishable from "
                "an untouched one while the truth record still counts towards "
                "the detection denominator (A ruling on PR #27)."
            )
        end = start + duration
        if total_points is not None and end > total_points:
            raise ValueError(
                f"{path} ends at {end}, outside scenario.total_points="
                f"{total_points}"
            )
        events.append(
            FaultEvent(
                event_id=event_id,
                device_id=device_id,
                type=kind,
                start_seq=start,
                end_seq_exclusive=end,
                amplitude=amplitude,
            )
        )
    return events


def _chunk_layout(chunk: object) -> tuple[np.ndarray, int, int]:
    """Validate the chunk arrays and return ``(vibration, start_seq, count)``.

    Args:
        chunk: Object carrying ``seq``, ``timestamp_ns``, ``vibration`` and
            ``temperature`` as equal-length 1-D arrays.

    Returns:
        The ``float64`` vibration array, the absolute index of its first
        sample, and its length.

    Raises:
        ValueError: if a field is missing, lengths disagree, dtypes are not the
            frozen ones, or ``seq`` is not contiguous.
    """
    arrays: dict[str, np.ndarray] = {}
    for name in ("seq", "timestamp_ns", "vibration", "temperature"):
        value = getattr(chunk, name, None)
        if value is None:
            raise ValueError(f"chunk has no field {name!r}")
        array = np.asarray(value)
        if array.ndim != 1:
            raise ValueError(f"chunk.{name} must be one-dimensional, got {array.ndim}")
        arrays[name] = array

    lengths = {int(array.shape[0]) for array in arrays.values()}
    if len(lengths) > 1:
        raise ValueError(f"chunk arrays must be equal length, got {sorted(lengths)}")
    if arrays["vibration"].dtype != np.float64:
        raise ValueError(
            f"chunk.vibration must be float64, got {arrays['vibration'].dtype}"
        )
    if arrays["seq"].dtype != np.int64:
        raise ValueError(f"chunk.seq must be int64, got {arrays['seq'].dtype}")

    seq = arrays["seq"]
    count = int(seq.shape[0])
    if count and not bool(np.all(np.diff(seq) == 1)):
        raise ValueError("chunk.seq must be contiguous and increasing by 1")
    start = int(seq[0]) if count else 0
    return arrays["vibration"], start, count


def _resolve_settings(config: object) -> tuple[list[FaultEvent], int]:
    """Read the injection-relevant part of the scenario configuration.

    Args:
        config: ``ScenarioConfig``, mapping or object, as produced by C3.

    Returns:
        The configured events and ``scenario.sample_rate_hz``.

    Raises:
        ValueError: for an unknown mode, an out-of-range probability, or an
            invalid event list.
        NotImplementedError: when a non-zero stochastic probability is
            requested, because the event distribution is not part of the
            V1.0 configuration contract yet.
    """
    scenario = _nested(config, "scenario")
    generation = _nested(config, "fault_generation")

    mode = str(_field(generation, "mode", "fault_generation.mode", "manual")).strip()
    if mode not in _MODES:
        raise ValueError(
            f"unsupported fault_generation.mode {mode!r}; expected one of {_MODES}"
        )
    probability = _as_finite_float(
        _field(
            generation,
            "stochastic_probability_per_window",
            "fault_generation.stochastic_probability_per_window",
            0.0,
        ),
        "fault_generation.stochastic_probability_per_window",
    )
    if not 0.0 <= probability <= 1.0:
        raise ValueError(
            "stochastic_probability_per_window must be within [0, 1], got "
            f"{probability}"
        )
    if mode != "manual" and probability > 0.0:
        raise NotImplementedError(
            f"fault_generation.mode={mode!r} with probability {probability} is not "
            "implemented: the type/amplitude/duration distribution of stochastic "
            "events is not defined in configs V1.0 (Issue #6 open question). "
            "Use mode='manual' until an ADR freezes it."
        )

    sample_rate_hz = _as_int(
        _field(scenario, "sample_rate_hz", "scenario.sample_rate_hz"),
        "scenario.sample_rate_hz",
    )
    device_id = _field(scenario, "device_id", "scenario.device_id", "")
    total_points = _lookup(scenario, "total_points")
    total = (
        None
        if total_points is _ABSENT or total_points is None
        else _as_int(total_points, "scenario.total_points")
    )
    events = _parse_events(
        _field(generation, "events", "fault_generation.events", None),
        "" if device_id is None else str(device_id),
        total,
    )
    return events, sample_rate_hz


def _check_finite(values: np.ndarray, start_seq: int, events: list[FaultEvent]) -> None:
    """Raise if the injected signal holds a NaN or an infinity.

    Args:
        values: Injected vibration array.
        start_seq: Absolute index of ``values[0]``.
        events: Events applied, named in the error message.

    Raises:
        ValueError: on the first non-finite sample.
    """
    finite = np.isfinite(values)
    if bool(np.all(finite)):
        return
    local = int(np.argmax(~finite))
    identifiers = ", ".join(event.event_id for event in events)
    raise ValueError(
        f"fault injection produced a non-finite vibration value at sample index "
        f"{start_seq + local} (events: {identifiers}); check the amplitudes"
    )


def _apply_events(
    vibration: np.ndarray,
    chunk_start: int,
    count: int,
    events: Sequence[FaultEvent],
    sample_rate_hz: int,
) -> tuple[np.ndarray | None, list[FaultEvent]]:
    """Accumulate every intersecting event into one copy of the signal.

    Args:
        vibration: Clean ``float64`` vibration array of the chunk.
        chunk_start: Absolute index of ``vibration[0]``.
        count: Number of samples in the chunk.
        events: All configured events, in configuration order.
        sample_rate_hz: Sampling frequency, used by ``burst``.

    Returns:
        ``(None, [])`` when no event intersects; otherwise the injected copy
        and the events that touched this chunk, each with its full global
        interval.
    """
    chunk_stop = chunk_start + count
    injected: np.ndarray | None = None
    truth: list[FaultEvent] = []
    for event in events:
        low = max(event.start_seq, chunk_start)
        high = min(event.end_seq_exclusive, chunk_stop)
        if low >= high:
            continue
        truth.append(event)
        if injected is None:
            injected = np.array(vibration, dtype=np.float64, copy=True)
        injected[low - chunk_start : high - chunk_start] += _waveform(
            event, low, high, sample_rate_hz
        )
    return injected, truth


def inject_faults(
    chunk: "SimulationChunk",
    config: object,
    rng: np.random.Generator | None = None,
) -> tuple["SimulationChunk", list[FaultEvent]]:
    """Inject the configured events into one chunk of simulated signal.

    Args:
        chunk: Simulated shard from
            :func:`g15.simulation.engine.simulate_chunk`. It is never modified;
            the returned chunk is a new instance when anything was injected.
        config: ``ScenarioConfig``; only ``scenario`` (``sample_rate_hz``,
            ``device_id``, ``total_points``) and ``fault_generation`` are read.
        rng: Accepted for signature compatibility with ``docs/interface.md``
            section 6 and type-checked, but intentionally not consumed: the
            three waveforms are deterministic functions of the absolute sample
            index, so chunk order and chunk size cannot change the result.

    Returns:
        A tuple ``(injected_chunk, events)``. ``events`` holds one
        :class:`FaultEvent` per event intersecting this chunk, with its **full
        global** interval and in configuration order; it is empty when nothing
        intersects, in which case the input chunk object is returned as-is.

    Raises:
        TypeError: if ``rng`` is not a NumPy generator, or ``chunk`` is not a
            dataclass.
        ValueError: for an invalid event, a malformed chunk, or a non-finite
            injection result.
        NotImplementedError: for an unimplemented stochastic schedule.
    """
    if rng is not None and not isinstance(rng, np.random.Generator):
        raise TypeError(
            f"rng must be a numpy.random.Generator, got {type(rng).__name__}"
        )

    vibration, chunk_start, count = _chunk_layout(chunk)
    events, sample_rate_hz = _resolve_settings(config)
    if not count:
        return chunk, []

    injected, truth = _apply_events(
        vibration, chunk_start, count, events, sample_rate_hz
    )
    if injected is None:
        return chunk, truth

    _check_finite(injected, chunk_start, truth)
    try:
        result = dataclasses.replace(chunk, vibration=injected)
    except TypeError as exc:  # pragma: no cover - defensive
        raise TypeError(
            "chunk must be a dataclass exposing seq/timestamp_ns/vibration/"
            f"temperature, got {type(chunk).__name__}"
        ) from exc
    return result, truth
