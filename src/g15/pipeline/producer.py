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
        ``scenario.id`` when present, otherwise ``""``.
    """
    value = _field(config, "id", None)
    return "" if value is None else str(value)


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
    """Return the manifest ``units`` mapping, preferring the configuration.

    Args:
        config: Scenario configuration.

    Returns:
        A mapping with exactly ``vibration`` and ``temperature``.
    """
    units: dict[str, str] = {}
    for signal, default in FIELD_UNITS.items():
        declared = _field(config, "unit", default, block=f"signals.{signal}")
        units[signal] = declared if isinstance(declared, str) and declared else default
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
    """Return C1's injection function, or ``None`` while Issue #6 is unmerged.

    ``g15.simulation.faults`` is owned by C1 and is an empty placeholder on the
    current ``main``.  Until it lands, the producer must run *without* injection
    rather than pretend it injected anything: it logs one warning per run,
    returns ``None`` and reports no events.

    Returns:
        ``inject_faults`` when importable, otherwise ``None``.
    """
    try:
        # Local import: C1's module is not on ``main`` yet, so a module-level
        # import would make the whole producer unusable until Issue #6 merges.
        # pylint: disable=import-outside-toplevel
        from g15.simulation.faults import inject_faults
    except ImportError:
        _LOG.warning(
            "fault injection unavailable: g15.simulation.faults is not "
            "implemented yet (Issue #6); this run contains no injected events"
        )
        return None
    return inject_faults


def _write_parts(
    config: object,
    run_dir: Path,
    total_points: int,
    chunk_points: int,
    hook: Callable[..., tuple[Any, list[Any]]] | None,
) -> tuple[list[ChunkManifestEntry], dict[str, Any]]:
    """Generate and publish every shard of the run.

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
) -> Manifest:
    """Assemble the ``complete`` manifest for a finished run.

    Args:
        config: Scenario configuration.
        params: Validated scenario parameters from the simulation kernel.
        run_id: Identifier of the run directory.
        entries: Shards in publication order.

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
        units=_units_from(config),
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
        ValueError: if a scenario field is missing or not a positive integer,
            or the simulation kernel rejects the configuration.
        OSError: if the run directory cannot be created or written.
        g15.pipeline.schema.DataContractError: if a shard or the manifest
            violates the contract; no ``complete`` manifest is published then.
    """
    params = load_scenario_parameters(config)
    chunk_points = _positive_int(
        _field(config, "chunk_points"), "scenario.chunk_points"
    )

    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = make_run_id(_scenario_id(config))
    run_dir = root / run_id
    run_dir.mkdir(exist_ok=False)

    hook = _fault_hook()

    # --- official measurement window (acceptance.md section 2) starts here ---
    started = time.perf_counter()
    entries, truth = _write_parts(
        config, run_dir, params.total_points, chunk_points, hook
    )
    manifest = _build_manifest(config, params, run_id, entries)
    _write_json_atomic(run_dir / "manifest.json", manifest.to_dict())
    elapsed = time.perf_counter() - started
    # --- official measurement window ends here ---

    fault_events = _write_truth(run_dir, run_id, truth)
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
