"""Consume a published run into SQLite (Issue #8, owner C2).

This is the consumer entry point named in ``docs/interface.md`` section 6. It
reads a run directory produced by C4's producer, analyses every window through
B2's ``analysis`` layer, and writes the results into the five tables of
``docs/interface.md`` section 9.

What the consumer guarantees
----------------------------

**One transaction per shard.** The features, the score, the alarms, the alarm
state and the chunk marker are written inside a single transaction. A crash
can never leave a shard marked as processed while its features are missing,
and can never leave features without their chunk marker.

**Idempotency.** Re-consuming a run does not duplicate anything. A shard that
was already processed with the same digest is skipped -- but its *state* is
still loaded, so the alarm streak continues across the shard boundary instead
of restarting.

**Refusal over guessing.** A shard whose digest changed under the same part id
is refused. A state that cannot be attributed to the current rules is refused.
A gap or an overlap in the sample sequence is refused. A shard layout that
cannot be cut into whole windows is refused *before* anything is written. A
run whose stored windows were cut on a different grid is refused. The consumer
stops and says why rather than writing something plausible.

What the consumer never does
----------------------------

It never reads ``private_truth/`` and never reads a producer scenario
configuration. The consumer configuration carries ``window_points``,
``incomplete_window`` and the health rules path -- nothing that says where a
fault was injected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from g15.analysis.alarms import Alarm, AlarmState, evaluate_alarm
from g15.analysis.features import FeatureRecord, extract_features
from g15.analysis.health import (
    HealthResult,
    HealthRules,
    load_health_rules,
    score_health,
)
from g15.common.types import SimulationChunk
from g15.config import ConsumerConfig
from g15.pipeline.schema import ChunkManifestEntry, Manifest, ManifestStatus
from g15.pipeline.serialization import read_chunk
from g15.storage.database import connect, initialize_database, transaction
from g15.storage.repository import (
    ConsumptionRepository,
    check_chunk_digest,
    encode_features_json,
    encode_state_json,
    utc_now_text,
)

__all__ = [
    "ConsumptionError",
    "ConsumptionSummary",
    "consume_run",
    "load_manifest_from_run_dir",
]


class ConsumptionError(Exception):
    """Raised when a run cannot be consumed.

    The consumer maps this onto the CLI exit code for I/O and schema failures
    (exit code 3 in ``docs/interface.md`` section 10).
    """


@dataclass(frozen=True)
class ConsumptionSummary:
    """What one consumption pass did.

    Attributes:
        run_id: The consumed run.
        device_id: The device the run belongs to.
        windows_written: Windows written by this pass.
        alarms_written: Alarms written by this pass.
        shards_processed: Shards analysed by this pass.
        shards_skipped: Shards already present with the same digest.
        windows_dropped: Samples discarded because they did not fill a whole
            window. Despite the name this counts *samples*, not windows: the
            name is kept because it is part of the reviewed interface, and the
            only legal source of dropped samples is the final shard's tail
            under ``incomplete_window: drop``.
    """

    run_id: str
    device_id: str
    windows_written: int = 0
    alarms_written: int = 0
    shards_processed: int = 0
    shards_skipped: int = 0
    windows_dropped: int = 0


@dataclass(frozen=True)
class _Window:
    """One window cut out of a shard, before analysis.

    Attributes:
        features: The window's features.
        start_seq: Absolute sequence number of the window's first sample.
        end_seq_exclusive: One past the window's last sample.
    """

    features: FeatureRecord
    start_seq: int
    end_seq_exclusive: int


@dataclass(frozen=True)
class _Analysed:
    """One analysed window.

    Attributes:
        window: The window that was analysed.
        health: Its health result.
        alarms: The alarms it raised.
        state: The alarm state after the window.
    """

    window: _Window
    health: HealthResult
    alarms: tuple[Alarm, ...]
    state: AlarmState


@dataclass
class _RunContext:
    """Mutable state carried across the shards of one run.

    A flat counter bag is deliberate: splitting it into sub-objects would put
    the shard-to-shard bookkeeping behind two hops for no gain.

    Attributes:
        state: The alarm state carried from the previous shard.
        windows_written: Windows written so far.
        alarms_written: Alarms written so far.
        shards_processed: Shards analysed so far.
        shards_skipped: Shards skipped so far.
        windows_dropped: Tail samples discarded so far.
        previous_chunk_last_seq: The last sequence number of the previous
            shard, used to detect a gap between shards.
        committed_end_seq: The end position of the last committed window,
            used to check that a resumed run continues where it stopped.
        origin_checked: Whether the first analysed shard has been compared
            with the run's declared origin (``seq = 0``).
    """

    # pylint: disable=too-many-instance-attributes

    state: AlarmState
    windows_written: int = 0
    alarms_written: int = 0
    shards_processed: int = 0
    shards_skipped: int = 0
    windows_dropped: int = 0
    previous_chunk_last_seq: int | None = None
    committed_end_seq: int | None = None
    origin_checked: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def load_manifest_from_run_dir(run_dir: Path) -> Manifest:
    """Read and validate the manifest of a run directory.

    Args:
        run_dir: The run directory.

    Returns:
        The validated manifest.

    Raises:
        ConsumptionError: if the manifest is missing or malformed.
    """
    from g15.pipeline.schema import manifest_from_dict

    manifest_path = Path(run_dir) / "manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConsumptionError(f"no manifest at {manifest_path}") from exc
    except (OSError, ValueError) as exc:
        raise ConsumptionError(f"cannot read {manifest_path}: {exc}") from exc
    try:
        return manifest_from_dict(raw)
    except Exception as exc:  # noqa: BLE001 - re-raised as a consumer error
        raise ConsumptionError(f"invalid manifest at {manifest_path}: {exc}") from exc


def consume_run(
    run_dir: Path,
    database_path: Path,
    config: ConsumerConfig,
) -> ConsumptionSummary:
    """Consume one run into the database.

    Args:
        run_dir: The run directory to consume. It must hold a manifest with
            ``status=complete``.
        database_path: Where the SQLite database lives. It is created if it
            does not exist.
        config: The consumer configuration.

    Returns:
        A summary of what was written.

    Raises:
        ConsumptionError: if the run cannot be consumed -- the manifest is not
            complete, a shard is untrustworthy, the sample sequence has a gap
            or an overlap, or the stored alarm state is incompatible with the
            current rules.
    """
    run_path = Path(run_dir)
    manifest = load_manifest_from_run_dir(run_path)

    if manifest.status is not ManifestStatus.COMPLETE:
        raise ConsumptionError(
            f"run {manifest.run_id!r} has status {manifest.status.value!r}; only "
            f"a 'complete' run may be consumed"
        )

    rules = load_health_rules(config.health_rules_path)

    connection = connect(database_path)
    try:
        initialize_database(connection)
        repository = ConsumptionRepository(connection)
        return _consume_manifest(
            repository=repository,
            manifest=manifest,
            run_dir=run_path,
            rules=rules,
            window_points=config.window_points,
            incomplete_window=config.incomplete_window,
            sample_rate_hz=manifest.sample_rate_hz,
        )
    finally:
        connection.close()


def _consume_manifest(
    *,
    repository: ConsumptionRepository,
    manifest: Manifest,
    run_dir: Path,
    rules: HealthRules,
    window_points: int,
    incomplete_window: str,
    sample_rate_hz: int,
) -> ConsumptionSummary:
    """Consume every shard of a validated manifest."""
    run_id = manifest.run_id
    device_id = manifest.device_id

    # Refuse an illegal grid before the first write. ``interface.md`` section
    # 8.1 requires every non-final shard to be a whole number of windows, so a
    # shard length that does not divide ``window_points`` is a contract
    # violation, not something to absorb by dropping the tail of *every*
    # shard. Cutting the remainder off each shard would silently discard a
    # slice per shard and put the window starts off the global grid, while
    # still returning success -- exactly the "plausible but wrong" outcome
    # this consumer exists to avoid.
    _check_window_grid(manifest.parts, window_points)

    stored_width = repository.get_stored_window_points(run_id)
    if stored_width is not None and stored_width != window_points:
        raise ConsumptionError(
            f"run {run_id!r} already holds windows of {stored_width} samples but "
            f"window_points={window_points} is requested; the stored analysis was "
            f"produced on a different grid. Consume into a new database, or reset "
            f"the run explicitly if the change is intended"
        )

    repository.ensure_run(
        run_id=run_id,
        scenario_id=manifest.scenario_id,
        schema_version=manifest.schema_version,
        scoring_version=rules.scoring_version,
        status=manifest.status.value,
        started_at=manifest.start_time_utc,
    )

    # The streak continues from the committed state, never from process memory.
    stored = repository.get_alarm_state(run_id, device_id, rules.rule_id)
    if stored is None:
        stored = AlarmState()
    last_end = repository.get_last_end_seq_exclusive(run_id, device_id, rules.rule_id)

    context = _RunContext(state=stored, committed_end_seq=last_end)

    for entry in manifest.parts:
        _consume_part(
            repository=repository,
            context=context,
            manifest=manifest,
            entry=entry,
            run_dir=run_dir,
            rules=rules,
            window_points=window_points,
            incomplete_window=incomplete_window,
            sample_rate_hz=sample_rate_hz,
        )

    return ConsumptionSummary(
        run_id=run_id,
        device_id=device_id,
        windows_written=context.windows_written,
        alarms_written=context.alarms_written,
        shards_processed=context.shards_processed,
        shards_skipped=context.shards_skipped,
        windows_dropped=context.windows_dropped,
    )


def _consume_part(
    *,
    repository: ConsumptionRepository,
    context: _RunContext,
    manifest: Manifest,
    entry: ChunkManifestEntry,
    run_dir: Path,
    rules: HealthRules,
    window_points: int,
    incomplete_window: str,
    sample_rate_hz: int,
) -> None:
    """Consume one shard, in one transaction."""
    run_id = manifest.run_id
    device_id = manifest.device_id

    recorded_sha = repository.get_processed_chunk_sha(run_id, entry.part_id)
    check_chunk_digest(recorded_sha, entry.sha256, entry.part_id)

    chunk = _read_part(run_dir, entry)

    if recorded_sha is not None:
        # The shard was already analysed. Skip its analysis, but keep the
        # streak alive: the state was loaded from the database and must not
        # be reset, otherwise the debounce counters would restart at every
        # shard boundary and the alarm logic would change meaning.
        context.shards_skipped += 1
        context.previous_chunk_last_seq = int(chunk.seq[-1])
        return

    _check_shard_continuity(context, chunk)

    windows, dropped = _build_windows(
        chunk=chunk,
        window_points=window_points,
        incomplete_window=incomplete_window,
        sample_rate_hz=sample_rate_hz,
    )
    context.windows_dropped += dropped

    # Pure computation first: analyse every window before touching the
    # database. If the analysis refuses the state or the window position,
    # nothing has been written yet.
    analysed = _analyse_windows(windows, rules, context.state)

    with transaction(repository.get_connection()):
        repository.mark_chunk_processed(
            run_id=run_id,
            part_id=entry.part_id,
            sha256=entry.sha256,
            processed_at=utc_now_text(),
        )
        for item in analysed:
            repository.insert_feature_window(
                run_id=run_id,
                device_id=device_id,
                start_seq=item.window.start_seq,
                end_seq_exclusive=item.window.end_seq_exclusive,
                features_json=encode_features_json(
                    item.window.features,
                    item.health,
                    window_start_seq=item.window.start_seq,
                ),
                health_score=float(item.health.health_score),
            )
            for alarm in item.alarms:
                repository.insert_alarm(
                    run_id=run_id,
                    device_id=device_id,
                    rule_id=alarm.rule_id,
                    start_seq=alarm.start_seq,
                    level=alarm.level,
                    reason=alarm.reason,
                    created_at=utc_now_text(),
                )
            repository.upsert_alarm_state(
                run_id=run_id,
                device_id=device_id,
                rule_id=rules.rule_id,
                last_end_seq_exclusive=item.window.end_seq_exclusive,
                state_json=encode_state_json(item.state),
            )

    # Only after the commit succeeded does the new state become the carried
    # state. If the commit failed, the in-memory state is discarded and the
    # next attempt rebuilds it from the committed row.
    if analysed:
        context.state = analysed[-1].state
        context.committed_end_seq = analysed[-1].window.end_seq_exclusive
    context.windows_written += len(analysed)
    context.alarms_written += sum(len(item.alarms) for item in analysed)
    context.shards_processed += 1
    context.previous_chunk_last_seq = int(chunk.seq[-1])


def _read_part(run_dir: Path, entry: ChunkManifestEntry) -> SimulationChunk:
    """Read and verify one shard, always with the strong ``run_dir`` check."""
    shard_path = run_dir / entry.path
    try:
        return read_chunk(shard_path, entry, run_dir=run_dir)
    except Exception as exc:  # noqa: BLE001 - re-raised as a consumer error
        raise ConsumptionError(
            f"shard {entry.part_id} ({entry.path}) is not trustworthy: {exc}"
        ) from exc


def _check_shard_continuity(context: _RunContext, chunk: SimulationChunk) -> None:
    """Check that the shard continues the run without a gap or an overlap."""
    if len(chunk) == 0:
        raise ConsumptionError("shard is empty; an empty shard is refused")

    first_seq = int(chunk.seq[0])

    # A run starts at sample 0 (``interface.md`` section 5.1). Checking this
    # only when nothing has been committed yet keeps resumed runs working: a
    # resumed run legitimately starts at the last committed position.
    if (
        not context.origin_checked
        and context.previous_chunk_last_seq is None
        and context.committed_end_seq is None
        and first_seq != 0
    ):
        raise ConsumptionError(
            f"the run starts at seq {first_seq} instead of 0; a run begins at "
            f"sample 0, and a shifted first shard would move every window "
            f"position of the run"
        )
    context.origin_checked = True

    previous_last = context.previous_chunk_last_seq
    if previous_last is not None and first_seq != previous_last + 1:
        raise ConsumptionError(
            f"sample sequence has a gap or an overlap between shards: the "
            f"previous shard ended at {previous_last} and this one starts at "
            f"{first_seq}; expected {previous_last + 1}"
        )

    # Within a shard the sequence must be contiguous as well.
    deltas = np.diff(chunk.seq)
    if deltas.size and not bool(np.all(deltas == 1)):
        bad = int(np.argmin(deltas != 1))
        raise ConsumptionError(
            f"sample sequence is not contiguous inside the shard: seq jumps "
            f"from {int(chunk.seq[bad])} to {int(chunk.seq[bad + 1])}"
        )

    # A resumed run must continue exactly where the committed windows ended.
    if (
        context.committed_end_seq is not None
        and context.previous_chunk_last_seq is None
    ):
        if first_seq != context.committed_end_seq:
            raise ConsumptionError(
                f"the run resumes at {first_seq} but the last committed window "
                f"ended at {context.committed_end_seq}; expected the shards to "
                f"continue from there"
            )


def _check_window_grid(parts: Sequence[ChunkManifestEntry], window_points: int) -> int:
    """Refuse a shard layout that cannot be cut into whole windows.

    ``interface.md`` section 8.1 requires ``chunk_points % window_points == 0``
    for every non-final shard and section 4 requires the consumer's entry point
    to check it. The manifest carries ``rows`` per shard rather than
    ``chunk_points``, so the nominal shard length is read back from the shards
    that are not the tail: those must all have the same length.

    This runs before any write, so an illegal layout leaves the database
    exactly as it was -- not even a ``runs`` row.

    Args:
        parts: The manifest's shards, in ascending ``part_id`` order.
        window_points: The configured window width in samples.

    Returns:
        The shard length every non-final shard shares, or ``0`` for a run with
        a single shard (which is the tail itself and has no non-final part).

    Raises:
        ConsumptionError: if ``window_points`` is not positive, the non-final
            shards disagree in length, or their length is not a multiple of
            ``window_points``.
    """
    if window_points <= 0:
        raise ConsumptionError(f"window_points must be positive, got {window_points}")
    if len(parts) <= 1:
        return 0

    non_final = list(parts[:-1])
    lengths = {entry.rows for entry in non_final}
    if len(lengths) != 1:
        detail = ", ".join(
            f"part {entry.part_id}: {entry.rows} rows" for entry in non_final
        )
        raise ConsumptionError(
            f"the manifest's non-final shards do not all have the same length "
            f"({detail}); a varying shard length would put the window grid off "
            f"the global sample positions. Nothing was written"
        )

    chunk_points = lengths.pop()
    if chunk_points % window_points == 0:
        return chunk_points

    blame = next(entry for entry in non_final if entry.rows == chunk_points)
    raise ConsumptionError(
        f"part {blame.part_id} holds {blame.rows} rows, which is not a multiple "
        f"of window_points={window_points} ({blame.rows % window_points} samples "
        f"left over); section 8.1 requires every non-final shard to be a whole "
        f"number of windows, so cutting the remainder off would drop samples at "
        f"every shard boundary. Nothing was written"
    )


def _build_windows(
    *,
    chunk: SimulationChunk,
    window_points: int,
    incomplete_window: str,
    sample_rate_hz: int,
) -> tuple[tuple[_Window, ...], int]:
    """Cut a shard into complete windows.

    Only the final shard may leave a remainder: :func:`_check_window_grid` has
    already refused a layout whose non-final shards are not whole windows, so
    a remainder here is the run's tail and nothing else.

    The tail is dropped under ``incomplete_window: drop`` and refused under
    ``incomplete_window: error`` -- the two policies of
    ``configs/consumer.yaml``.

    Returns:
        The windows and the number of dropped **samples** (not windows).
    """
    if window_points <= 0:
        raise ConsumptionError(f"window_points must be positive, got {window_points}")

    count = len(chunk)
    usable = count - (count % window_points)
    dropped = count - usable

    if dropped and incomplete_window == "error":
        raise ConsumptionError(
            f"the final shard holds {count} samples, which is not a multiple of "
            f"window_points={window_points}; the incomplete_window policy is "
            f"'error', so the {dropped}-sample tail is refused rather than "
            f"dropped"
        )

    records: list[_Window] = []
    for offset in range(0, usable, window_points):
        start = offset
        stop = offset + window_points
        start_seq = int(chunk.seq[start])
        features = extract_features(
            chunk.vibration[start:stop],
            sample_rate_hz=sample_rate_hz,
            window_start_seq=start_seq,
        )
        records.append(
            _Window(
                features=features,
                start_seq=start_seq,
                end_seq_exclusive=start_seq + window_points,
            )
        )

    return tuple(records), dropped


def _analyse_windows(
    windows: Sequence[_Window],
    rules: HealthRules,
    initial_state: AlarmState,
) -> list[_Analysed]:
    """Run the analysis chain over the windows, carrying the state.

    Args:
        windows: The windows to analyse.
        rules: The health rules.
        initial_state: The state carried in from the previous shard.

    Returns:
        One entry per window.
    """
    results: list[_Analysed] = []
    state = initial_state
    for window in windows:
        health = score_health(window.features, rules)
        alarms, new_state = evaluate_alarm(health, state, rules)
        results.append(
            _Analysed(
                window=window, health=health, alarms=tuple(alarms), state=new_state
            )
        )
        state = new_state
    return results
