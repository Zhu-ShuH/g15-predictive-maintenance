"""Produce a complete run directory: shards first, then one ``complete`` manifest.

This module is the only entry point that turns a scenario configuration into the
on-disk artefacts a consumer reads.  The publication order is the whole point:

1. Every shard is written atomically by the serialisation layer.
2. Only after *all* shards exist is ``manifest.json`` written, also atomically,
   with ``status="complete"``.

A reader that follows ``docs/interface.md`` section 5.2 therefore never observes
a half-written run: either the manifest is missing (nothing to consume) or it
describes a run whose parts are all present and whose row counts add up.  The
:class:`~g15.pipeline.schema.Manifest` type enforces that last invariant itself.

Shard boundaries
----------------
``chunk_points`` is a *nominal* shard size, not a divisibility requirement.
``total_points`` that is not a multiple of ``chunk_points`` ends with one short
tail shard.  Rounding such a run up and padding it with samples that were never
configured would fabricate data, so the tail is kept and documented instead:

    shard_count  = ceil(total_points / chunk_points)
    rows(k)      = chunk_points                        for k < shard_count - 1
    rows(last)   = total_points - (shard_count - 1) * chunk_points

The tail is an ordinary shard in every other respect: it still has
``rows >= 1``, a contiguous ``part_id`` and a real SHA-256, and the manifest
still covers ``total_points`` exactly.  ``docs/interface.md`` section 5.1 states
the same rule, and ``docs/acceptance.md`` section 2 relies on it for the
benchmark shape (9 x 102400 + 78400 = 1,000,000).  A configuration is therefore
never rejected for being non-divisible; the arithmetic is documented instead.

Timing
------
``docs/acceptance.md`` section 2 fixes the one legitimate 1,000,000-point
measurement window: it starts after the configuration has been validated and
before the first sample is generated, and it ends when the ``complete`` manifest
has been published and its file closed.  It therefore *includes* noise
generation, fault injection, serialisation, file writes and SHA-256, and
*excludes* interpreter start-up, the downstream consumer and reporting.
:func:`produce_run_detailed` returns that number; :func:`produce_run` is the
frozen ``docs/interface.md`` section 6 signature and discards it.

Timestamp range
---------------
``timestamp_ns`` is ``int64`` UTC Unix nanoseconds, so the run start instant
plus ``total_points / sample_rate_hz`` seconds must stay below ``2**63 - 1``.
The kernel computes the offset in ``int64`` and would wrap silently past that
point, producing negative timestamps for the tail of a long run.  A run that
cannot be represented is therefore refused *before* the run directory exists -
see :func:`_check_timestamp_range` - rather than published with corrupt clocks.

Fault truth
-----------
``private_truth/faults.json`` is written only when at least one event was
injected, and it is never read here.  The producer is allowed to *write* the
answer key; nothing downstream of this module is allowed to read it.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from g15.pipeline.schema import (
    FIELD_UNITS,
    SCHEMA_VERSION,
    ChunkManifestEntry,
    Manifest,
    ManifestStatus,
)
from g15.pipeline.serialization import write_chunk_atomic
from g15.simulation.engine import (
    NANOSECONDS_PER_SECOND,
    ScenarioParameters,
    load_scenario_parameters,
    simulate_chunk,
)

__all__ = [
    "PART_DIRECTORY",
    "TRUTH_DIRECTORY",
    "ProductionSummary",
    "make_run_id",
    "produce_run",
    "produce_run_detailed",
]

_LOG = logging.getLogger(__name__)

#: Sub-directory holding the NPZ shards (``docs/interface.md`` section 11).
PART_DIRECTORY: Final[str] = "raw"

#: Sub-directory holding the answer key; validator only.
TRUTH_DIRECTORY: Final[str] = "private_truth"

#: Sub-directory holding the run log.
LOG_DIRECTORY: Final[str] = "logs"

#: Shape of the UTC prefix of a run identifier, e.g. ``20261014T100000Z``.
_RUN_STAMP_FORMAT: Final[str] = "%Y%m%dT%H%M%SZ"

#: Characters allowed in the scenario part of a run identifier.
_UNSAFE_SLUG: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")

#: Bounds of an ``int64`` nanosecond timestamp (``-2**63`` .. ``2**63 - 1``).
#: ``SimulationChunk.timestamp_ns`` is ``int64`` (``docs/interface.md`` 5.1),
#: so these are hard limits, not policy choices.  The upper bound is exceeded by
#: a run that is too long; the lower bound is exceeded by a ``start_time_utc``
#: before 1677-09-21, which overflows on the *first* sample instead.
_INT64_MAX_NS: Final[int] = (1 << 63) - 1
_INT64_MIN_NS: Final[int] = -(1 << 63)

#: Human-readable form of :data:`_INT64_MIN_NS` / :data:`_INT64_MAX_NS`.
_TIMESTAMP_RANGE_TEXT: Final[str] = "1677-09-21T00:12:44Z .. 2262-04-11T23:47:16Z"


class _Absent:
    """Sentinel type distinguishing "field not present" from a stored ``None``."""

    # The identity of the sentinel is the whole contract.
    # pylint: disable=too-few-public-methods

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


_ABSENT: Final[object] = _Absent()


def _dig(source: object, dotted: str) -> Any:
    """Read a dotted path from nested mappings and/or object attributes.

    ``ScenarioConfig`` is owned by C3 and is not frozen yet (Issue #4 is open), so
    the producer reads it structurally, exactly as the simulation kernel does.

    Args:
        source: Configuration object or mapping.
        dotted: Path such as ``"scenario.chunk_points"``.

    Returns:
        The value, or the module sentinel when any segment is absent.
    """
    node: Any = source
    for segment in dotted.split("."):
        if node is None:
            return _ABSENT
        if isinstance(node, Mapping):
            if segment not in node:
                return _ABSENT
            node = node[segment]
        else:
            node = getattr(node, segment, _ABSENT)
            if node is _ABSENT:
                return _ABSENT
    return node


def _field(
    config: object,
    name: str,
    default: Any = _ABSENT,
    *,
    block: str = "scenario",
) -> Any:
    """Read ``block.name`` from the configuration, tolerating a flat layout.

    Args:
        config: Scenario configuration.
        name: Field name.
        default: Value returned when the field is absent.
        block: Nested block the field normally lives in.

    Returns:
        The field value, or ``default``.

    Raises:
        ValueError: if the field is absent and no default was supplied.
    """
    for path in (f"{block}.{name}", name):
        value = _dig(config, path)
        if value is not _ABSENT:
            return value
    if default is not _ABSENT:
        return default
    raise ValueError(f"{block}.{name} is missing from the scenario configuration")


def _positive_int(value: object, path: str) -> int:
    """Return ``value`` as a positive ``int``.

    Args:
        value: Candidate value.
        path: Dotted path used in the error message.

    Returns:
        The value as a Python ``int``.

    Raises:
        ValueError: if the value is not a positive integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer, got {value!r}")
    if value <= 0:
        raise ValueError(f"{path} must be positive, got {value}")
    return value


def _last_timestamp_ns(
    total_points: int, start_time_utc_ns: int, sample_rate_hz: int
) -> int:
    """Return the timestamp of the final sample, in exact Python integers.

    This mirrors :func:`g15.simulation.engine.timestamp_ns_for` step by step,
    but evaluates it in arbitrary-precision integers so the caller can compare
    the result with the ``int64`` limit before any array exists.

    Args:
        total_points: Number of samples in the run; must be positive.
        start_time_utc_ns: Run start instant as UTC Unix nanoseconds.
        sample_rate_hz: Sampling frequency in hertz; must be positive.

    Returns:
        The timestamp of sample ``total_points - 1`` in nanoseconds.
    """
    whole_seconds, remainder = divmod(total_points - 1, sample_rate_hz)
    sub_second_ns = (
        remainder * NANOSECONDS_PER_SECOND + sample_rate_hz // 2
    ) // sample_rate_hz
    return start_time_utc_ns + whole_seconds * NANOSECONDS_PER_SECOND + sub_second_ns


def _timestamp_offset_fits(
    sample_index: int, start_time_utc_ns: int, sample_rate_hz: int
) -> bool:
    """Return whether ``sample_index`` maps to an ``int64``-representable stamp.

    Mirrors ``engine.timestamp_ns_for`` step by step, but evaluates it in
    arbitrary-precision integers so the answer is known before any array
    exists.

    Two sums are required to fit: the relative offset and the final stamp.  The
    offset check makes this slightly *stricter* than the kernel for start
    instants before 1970.  The kernel's offset is an ``int64`` array that wraps
    modulo ``2**64``, and because the final stamp still lands in range the
    wrapped intermediate happens to subtract back out to the right value.  That
    is arithmetic luck, not a contract: a guard whose correctness depends on
    modular wraparound breaks the moment the kernel switches to object dtype or
    a checked loop.  Refusing a pre-1970 run longer than 292 years is a cheaper
    failure than silently trusting the wrap.

    Args:
        sample_index: Zero-based absolute sampling index.
        start_time_utc_ns: Run start instant as UTC Unix nanoseconds.
        sample_rate_hz: Sampling frequency in hertz; must be positive.

    Returns:
        True when the offset and the resulting stamp both stay inside ``int64``.
    """
    whole_seconds, remainder = divmod(sample_index, sample_rate_hz)
    sub_second_ns = (
        remainder * NANOSECONDS_PER_SECOND + sample_rate_hz // 2
    ) // sample_rate_hz
    offset_ns = whole_seconds * NANOSECONDS_PER_SECOND + sub_second_ns
    if offset_ns > _INT64_MAX_NS:
        return False
    stamp = start_time_utc_ns + offset_ns
    return _INT64_MIN_NS <= stamp <= _INT64_MAX_NS


def _max_total_points(start_time_utc_ns: int, sample_rate_hz: int) -> int:
    """Largest ``total_points`` whose last timestamp still fits in ``int64``.

    The predicate is monotone in the sample index, so the bound is found by
    bisection.  Exactness matters: an arbitrarily conservative bound would
    reject configurations that are legal, so the search is over the predicate
    itself rather than over a closed-form estimate.  The only deliberate slack
    is the pre-1970 offset case described on :func:`_timestamp_offset_fits`.

    Args:
        start_time_utc_ns: Run start instant as UTC Unix nanoseconds.
        sample_rate_hz: Sampling frequency in hertz; must be positive.

    Returns:
        The sample count that still fits, or ``0`` when even the first sample
        would overflow.
    """
    if not _timestamp_offset_fits(0, start_time_utc_ns, sample_rate_hz):
        return 0
    # Guaranteed not to fit: one well past the int64 horizon at this rate.
    high = (
        (_INT64_MAX_NS - start_time_utc_ns) // NANOSECONDS_PER_SECOND + 2
    ) * sample_rate_hz + 2
    low = 0
    while low < high:
        middle = (low + high + 1) // 2
        if _timestamp_offset_fits(middle, start_time_utc_ns, sample_rate_hz):
            low = middle
        else:
            high = middle - 1
    return low + 1


def _check_timestamp_range(params: ScenarioParameters) -> None:
    """Refuse a run whose timestamps cannot be represented in ``int64``.

    ``simulate_chunk`` computes ``start + seq / rate`` in ``int64`` arithmetic,
    which wraps silently instead of raising.  A long enough run therefore
    produces negative or non-monotonic ``timestamp_ns`` values that still pass
    every shape and finiteness check.  This guard converts that silent
    corruption into an immediate ``ValueError``, before the run directory is
    created, so no shard and no manifest is ever published.

    Args:
        params: Validated scenario parameters.

    Raises:
        ValueError: if ``start_time_utc`` itself lies outside the ``int64``
            nanosecond range, or if ``total_points`` exceeds what that range
            can represent for this start instant and sampling rate.
    """
    limit = _max_total_points(params.start_time_utc_ns, params.sample_rate_hz)
    if limit == 0:
        raise ValueError(
            "scenario.start_time_utc is outside the int64 UTC nanosecond range "
            f"[{_INT64_MIN_NS}, {_INT64_MAX_NS}] "
            f"({_TIMESTAMP_RANGE_TEXT}): start_time_utc_ns="
            f"{params.start_time_utc_ns}, so even the first sample would "
            "overflow"
        )
    if params.total_points <= limit:
        return
    worst = _last_timestamp_ns(
        params.total_points, params.start_time_utc_ns, params.sample_rate_hz
    )
    raise ValueError(
        f"scenario.total_points={params.total_points} overflows int64 UTC "
        f"nanoseconds: with sample_rate_hz={params.sample_rate_hz} and "
        f"start_time_utc_ns={params.start_time_utc_ns} the last timestamp would "
        f"be {worst} ns, but int64 stops at {_INT64_MAX_NS} ns (about 292 "
        f"years). At most {limit} points fit for this start instant and rate; "
        "shorten the run or move start_time_utc closer to 1970."
    )


def _scenario_slug(scenario_id: object) -> str:
    """Return a filesystem-safe slug for the run identifier.

    Args:
        scenario_id: Raw scenario identifier from the configuration.

    Returns:
        A slug containing only ``[A-Za-z0-9._-]``; ``"scenario"`` when empty.
    """
    text = "" if scenario_id is None else str(scenario_id)
    return _UNSAFE_SLUG.sub("-", text).strip("-.") or "scenario"


def _scenario_id(config: object) -> str:
    """Return the scenario identifier, falling back to the empty string.

    Args:
        config: Scenario configuration.

    Returns:
        ``scenario.id`` or the configuration object's ``scenario_id`` alias;
        the same names are also accepted on a flat configuration.
    """
    for name in ("id", "scenario_id"):
        value = _field(config, name, None)
        if value is not None:
            return str(value)
    return ""


def make_run_id(scenario_id: object, now: _dt.datetime | None = None) -> str:
    """Build ``<UTC stamp>-<scenario slug>-<6 hex>`` as in section 5.2.

    The random suffix keeps two runs of the same scenario in the same second
    apart, so ``output_root`` can be shared without collisions.

    Args:
        scenario_id: Scenario identifier.
        now: Instant to stamp; defaults to the current UTC time.

    Returns:
        A run identifier safe to use as a directory name.
    """
    moment = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    stamp = moment.astimezone(_dt.timezone.utc).strftime(_RUN_STAMP_FORMAT)
    return f"{stamp}-{_scenario_slug(scenario_id)}-{secrets.token_hex(3)}"


def _start_time_text(config: object, fallback_ns: int) -> str:
    """Return the manifest's ISO-8601 ``start_time_utc`` text.

    Args:
        config: Scenario configuration.
        fallback_ns: Start instant in UTC Unix nanoseconds, used when the
            configuration carries no readable text form.

    Returns:
        An ISO-8601 UTC timestamp ending in ``Z``.
    """
    raw = _field(config, "start_time_utc", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, _dt.datetime):
        moment = raw if raw.tzinfo else raw.replace(tzinfo=_dt.timezone.utc)
        return moment.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    moment = _dt.datetime.fromtimestamp(fallback_ns / 1e9, tz=_dt.timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def _units_from(config: object) -> dict[str, str]:
    """Return the manifest ``units`` mapping, validated against section 5.2.

    Section 5.2 freezes the unit strings (``g`` and ``degC``).  Copying an
    arbitrary declared unit such as ``m/s2`` into the manifest would publish
    valid-looking data that every consumer reads with the wrong magnitude, so a
    non-contract unit is refused instead of being silently relabelled.

    Args:
        config: Scenario configuration.

    Returns:
        A mapping with exactly ``vibration`` and ``temperature``.

    Raises:
        ValueError: if a declared unit is not the frozen one for its channel.
    """
    units: dict[str, str] = {}
    for signal, default in FIELD_UNITS.items():
        declared = _field(config, "unit", default, block=f"signals.{signal}")
        if not isinstance(declared, str) or declared != default:
            raise ValueError(
                f"signals.{signal}.unit must be {default!r} (docs/interface.md "
                f"section 5.2), got {declared!r}"
            )
        units[signal] = declared
    return units


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Serialise ``payload`` to ``path`` through a temporary file and a rename.

    Args:
        path: Destination file.
        payload: JSON-ready mapping.

    Raises:
        OSError: if the file cannot be written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _fault_hook() -> Callable[..., tuple[Any, list[Any]]] | None:
    """Return the fault-injection callable, or ``None`` if it cannot be imported.

    Issue #6 shipped ``g15.simulation.faults.inject_faults`` and was merged with
    ``#27``, so a missing import here is no longer an "unimplemented feature"
    condition - it means a broken or partial installation.  This function is
    kept as a **branch defence** so that such an installation still fails with a
    clear message instead of an ``ImportError`` traceback, and so that
    event-free runs keep working.  A configuration that actually asks for
    injected faults is rejected by :func:`produce_run_detailed` before any
    output is written.

    Returns:
        ``inject_faults`` when importable, otherwise ``None``.
    """
    try:
        # Local import keeps the producer importable on its own and lets this
        # function double as the availability probe.
        # pylint: disable=import-outside-toplevel
        from g15.simulation.faults import inject_faults
    except ImportError as exc:
        _LOG.warning(
            "fault injection unavailable: cannot import "
            "g15.simulation.faults.inject_faults (%s). Issue #6 shipped in #27, "
            "so this points at a broken installation rather than a missing "
            "feature; only event-free configurations can run.",
            exc,
        )
        return None
    return inject_faults


def _requires_fault_injection(config: object) -> bool:
    """Return whether the configuration requests any injected faults.

    Args:
        config: Validated mapping or configuration object.

    Returns:
        True for manual events or a non-zero stochastic probability.
    """
    events = _field(config, "events", (), block="fault_generation")
    probability = _field(
        config, "stochastic_probability_per_window", 0.0, block="fault_generation"
    )
    return bool(events) or probability != 0.0


def _write_parts(
    config: object,
    run_dir: Path,
    total_points: int,
    chunk_points: int,
    hook: Callable[..., tuple[Any, list[Any]]] | None,
) -> tuple[list[ChunkManifestEntry], dict[str, Any]]:
    """Generate and publish every shard of the run.

    ``chunk_points`` is nominal: a ``total_points`` that is not a multiple of it
    ends with one short tail shard of ``total_points % chunk_points`` rows.  The
    tail is never padded, never merged into its predecessor, and never dropped;
    it is published as an ordinary shard with a contiguous ``part_id`` and its
    own digest, and the manifest still covers ``total_points`` exactly.  See the
    module docstring for the closed form and ``docs/interface.md`` section 5.1
    for the contract.

    Args:
        config: Scenario configuration.
        run_dir: Run directory that receives ``raw/``.
        total_points: Number of samples the run must cover.
        chunk_points: Nominal shard size; the last shard may be shorter.
        hook: Fault-injection callable, or ``None`` when Issue #6 is unmerged.

    Returns:
        The manifest entries in publication order, plus the intersecting fault
        events keyed by ``event_id`` (empty when nothing was injected).

    Raises:
        ChunkValidationError: if a generated shard violates section 5.1.
        OSError, SerializationError: if a shard cannot be written.
    """
    raw_dir = run_dir / PART_DIRECTORY
    raw_dir.mkdir()

    entries: list[ChunkManifestEntry] = []
    truth: dict[str, Any] = {}
    start = 0
    part_id = 0
    while start < total_points:
        count = min(chunk_points, total_points - start)
        chunk = simulate_chunk(config, start, count)
        events: list[Any] = []
        if hook is not None:
            chunk, events = hook(chunk, config)
        for event in events:
            truth.setdefault(str(event.event_id), event)
        entry = write_chunk_atomic(
            chunk, raw_dir / f"part-{part_id:06d}.npz", run_dir=run_dir
        )
        _LOG.debug("published part %d: rows=%d", part_id, entry.rows)
        entries.append(entry)
        start += count
        part_id += 1
    return entries, truth


def _build_manifest(
    config: object,
    params: ScenarioParameters,
    run_id: str,
    entries: list[ChunkManifestEntry],
    units: dict[str, str],
) -> Manifest:
    """Assemble the ``complete`` manifest for a finished run.

    Args:
        config: Scenario configuration.
        params: Validated scenario parameters from the simulation kernel.
        run_id: Identifier of the run directory.
        entries: Shards in publication order.
        units: Already validated unit mapping (section 5.2).

    Returns:
        A manifest whose ``parts`` cover ``total_points`` exactly.
    """
    return Manifest(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        scenario_id=_scenario_id(config),
        device_id=params.device_id,
        seed=params.seed,
        sample_rate_hz=params.sample_rate_hz,
        start_time_utc=_start_time_text(config, params.start_time_utc_ns),
        total_points=params.total_points,
        units=dict(units),
        status=ManifestStatus.COMPLETE,
        parts=tuple(entries),
    )


def _write_truth(run_dir: Path, run_id: str, truth: dict[str, Any]) -> int:
    """Write ``private_truth/faults.json`` when events were injected.

    The file is ordered by ``start_seq`` then ``event_id`` so two runs of the
    same scenario produce byte-identical truth even if shards were retried.

    Args:
        run_dir: Run directory.
        run_id: Identifier of the run.
        truth: Intersecting events keyed by ``event_id``.

    Returns:
        The number of distinct events recorded.
    """
    if not truth:
        return 0
    ordered = sorted(
        truth.values(), key=lambda event: (event.start_seq, str(event.event_id))
    )
    _write_json_atomic(
        run_dir / TRUTH_DIRECTORY / "faults.json",
        {"run_id": run_id, "events": [event.to_dict() for event in ordered]},
    )
    return len(ordered)


def _record_completion(run_dir: Path, run_id: str, stats: Mapping[str, Any]) -> None:
    """Append the run-complete line to ``logs/app.jsonl`` (section 11).

    Logging must never invalidate an otherwise successful run, so a failure to
    write the line is reported through :mod:`logging` and swallowed.

    Args:
        run_dir: Run directory.
        run_id: Identifier of the run.
        stats: Measurement fields to record.
    """
    payload = {
        "timestamp_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "level": "INFO",
        "component": "producer",
        "run_id": run_id,
        "event": "run_complete",
        **stats,
    }
    path = run_dir / LOG_DIRECTORY / "app.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:  # pragma: no cover - defensive
        _LOG.warning("cannot append to %s: %s", path, exc)


@dataclass(frozen=True)
class ProductionSummary:
    """Outcome of one :func:`produce_run_detailed` call.

    Attributes:
        run_dir: Directory holding the published run.
        elapsed_seconds: The acceptance-section-2 measurement window.
        total_points: Samples in the run (the teacher's point count).
        scalar_value_count: Signal scalars written, i.e. twice ``total_points``.
        part_count: Number of shards published.
        fault_events: Number of distinct injected events recorded as truth.
    """

    run_dir: Path
    elapsed_seconds: float
    total_points: int
    scalar_value_count: int
    part_count: int
    fault_events: int


def produce_run_detailed(config: object, output_root: Path) -> ProductionSummary:
    """Generate a complete run and report the official timing window.

    Args:
        config: Scenario configuration from :mod:`g15.config` (mapping or
            object); read-only.
        output_root: Directory that receives a new ``<run_id>`` sub-directory.

    Returns:
        A :class:`ProductionSummary` describing the published run.

    Raises:
        ValueError: if a scenario field is missing or not a positive integer, a
            declared signal unit is not the frozen section 5.2 unit, or the
            simulation kernel rejects the configuration.
        OSError: if the run directory cannot be created or written.  A
            failure while writing the fault truth leaves the already written
            shards on disk but publishes **no** manifest, so the run is never
            consumable as complete (Issue #45).
        RuntimeError: if faults are requested but the injection module is
            unavailable; no output directory or complete manifest is published.
        g15.pipeline.schema.DataContractError: if a shard or the manifest
            violates the contract; no ``complete`` manifest is published then.
    """
    params = load_scenario_parameters(config)
    # Refused before any directory exists: an unrepresentable clock must not
    # leave a half-published run behind (see the module docstring).
    _check_timestamp_range(params)
    chunk_points = _positive_int(
        _field(config, "chunk_points"), "scenario.chunk_points"
    )
    # Validated before the run directory exists: a non-contract unit must fail
    # without publishing anything, not after the shards are already on disk.
    units = _units_from(config)

    hook = _fault_hook()
    if hook is None and _requires_fault_injection(config):
        raise RuntimeError(
            "configuration requires fault injection, but "
            "g15.simulation.faults.inject_faults is unavailable (Issue #6)"
        )

    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = make_run_id(_scenario_id(config))
    run_dir = root / run_id
    run_dir.mkdir(exist_ok=False)

    # --- official measurement window (acceptance.md section 2) starts here ---
    started = time.perf_counter()
    entries, truth = _write_parts(
        config, run_dir, params.total_points, chunk_points, hook
    )
    # The truth is written before the manifest on purpose (Issue #45): a
    # complete manifest must never outlive the truth it is supposed to
    # describe.  A failed truth write leaves the shards on disk but publishes
    # no manifest, which is exactly the semantics of a failed shard write --
    # the consumer rejects the run because the manifest is absent.
    fault_events = _write_truth(run_dir, run_id, truth)
    manifest = _build_manifest(config, params, run_id, entries, units)
    _write_json_atomic(run_dir / "manifest.json", manifest.to_dict())
    elapsed = time.perf_counter() - started
    # --- official measurement window ends here ---

    _record_completion(
        run_dir,
        run_id,
        {
            "elapsed_ms": round(elapsed * 1000.0, 3),
            "part_count": len(entries),
            "total_points": params.total_points,
            "fault_events": fault_events,
        },
    )
    _LOG.info(
        "run %s complete: %d parts, %d points, %.3f s",
        run_id,
        len(entries),
        params.total_points,
        elapsed,
    )

    return ProductionSummary(
        run_dir=run_dir,
        elapsed_seconds=elapsed,
        total_points=params.total_points,
        scalar_value_count=2 * params.total_points,
        part_count=len(entries),
        fault_events=fault_events,
    )


def produce_run(config: object, output_root: Path) -> Path:
    """Generate a complete run directory and return it.

    This is the frozen signature of ``docs/interface.md`` section 6.  Callers
    that need the official elapsed time (the benchmark command) use
    :func:`produce_run_detailed` instead.

    Args:
        config: Scenario configuration; read-only.
        output_root: Directory that receives a new ``<run_id>`` sub-directory.

    Returns:
        The published run directory.

    Raises:
        See :func:`produce_run_detailed`.
    """
    return produce_run_detailed(config, output_root).run_dir
