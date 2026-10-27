"""Independent post-production verification of a published run.

``docs/acceptance.md`` section 2 requires the acceptance run to be checked
*after* the producer's measurement window closes: point count, dtypes,
contiguous ``seq``, SHA-256 digests and the ``complete`` manifest.  This module
is that check and nothing else.

Why it is independent
---------------------
The verifier reads only ``run_dir``: the manifest and the shards it names.  It
never opens the SQLite database and never reads ``private_truth/``, so a run
whose answer key was deleted or whose database was never built still verifies
exactly the same way.  It also re-reads the NPZ archives itself instead of
delegating to :func:`g15.pipeline.serialization.read_chunk`, so a defect on the
consumer path cannot hide behind the same code being reused.  The only shared
pieces are the frozen constants (:data:`~g15.pipeline.schema.FIELD_DTYPES`,
:data:`~g15.pipeline.schema.NPZ_FIELDS`) and
:func:`~g15.pipeline.serialization.sha256_of_file`.

How failures are layered
------------------------
``docs/interface.md`` section 5.2 already refuses a structurally broken
manifest: a ``complete`` run with no parts, ``part_id`` values that are not
contiguous from zero, or ``rows`` that do not add up to ``total_points``.  Those
defects therefore surface here as :class:`VerificationError`, *not* as entries
in ``VerificationResult.failures`` -- reporting them twice would either mean
relaxing the public schema or pretending the manifest was readable when it was
not.

Once the manifest is readable and structurally valid, every remaining problem
is per-shard data damage: a missing file, an unreadable archive, a wrong field
set, a wrong dtype, a short or long shard, a broken ``seq`` run, or a digest
mismatch.  Those are collected in ``failures`` so that one pass reports them
all instead of stopping at the first one.
"""

from __future__ import annotations

import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

from g15.pipeline.consumer import ConsumptionError, load_manifest_from_run_dir
from g15.pipeline.schema import (
    FIELD_DTYPES,
    NPZ_FIELDS,
    ChunkManifestEntry,
    Manifest,
    ManifestStatus,
)
from g15.pipeline.serialization import SerializationError, sha256_of_file

__all__ = [
    "VERIFICATION_CHECKS",
    "ShardCheck",
    "VerificationError",
    "VerificationResult",
    "verify_run",
]

#: The checks one :func:`verify_run` pass performs, in reporting order.
#:
#: Callers (the CLI, the acceptance report) quote these names instead of
#: hard-coding their own strings, so "what was checked" cannot drift between
#: the verifier and whatever prints it.
VERIFICATION_CHECKS: Final[tuple[str, ...]] = (
    "manifest.complete",
    "parts.contiguous",
    "rows.sum",
    "shard.dtype",
    "shard.seq",
    "shard.hash",
    "seq.cross_shard",
)

#: Failure code used when a manifest entry names a file that is not there.
_MISSING: Final[str] = "shard.missing"
#: Failure code used when a shard exists but cannot be read as an NPZ archive.
_UNREADABLE: Final[str] = "shard.unreadable"
#: Failure code used when the archive does not carry exactly the four fields.
_FIELDS: Final[str] = "shard.fields"
#: Failure code used when the loaded object is not an NPZ archive at all.
#:
#: ``np.load`` happily returns a bare :class:`numpy.ndarray` for a ``.npy``
#: payload, so a file whose digest matches the manifest can still be the wrong
#: kind of container.  That is content damage, not a caller error.
_NOT_ARCHIVE: Final[str] = "shard.not_archive"
#: Failure code used when a field has the wrong dtype, shape or finiteness.
_DTYPE: Final[str] = "shard.dtype_mismatch"
#: Failure code used when the four arrays do not all have the same length.
_LENGTHS: Final[str] = "shard.length_mismatch"
#: Failure code used when the observed row count differs from the entry.
_ROWS: Final[str] = "shard.rows_mismatch"
#: Failure code used when ``seq`` does not advance by exactly one inside a shard.
_SHARD_SEQ: Final[str] = "shard.seq_gap"
#: Failure code used when the digest differs from the manifest entry.
_HASH: Final[str] = "shard.hash_mismatch"
#: Failure code used when the first shard does not start at ``seq`` zero.
_SEQ_ORIGIN: Final[str] = "seq.not_from_zero"
#: Failure code used when ``seq`` jumps between two consecutive shards.
_SEQ_CROSS: Final[str] = "seq.cross_shard_gap"
#: Failure code used when the run is not the contour the caller expected.
_EXPECTED: Final[str] = "rows.expected_mismatch"
#: Failure code used when the manifest does not describe a finished run.
_NOT_COMPLETE: Final[str] = "manifest.not_complete"


class VerificationError(Exception):
    """Raised when a run cannot be verified at all.

    Covers only the cases where verification is impossible rather than
    negative: the manifest is missing or structurally invalid, an entry path
    escapes the run directory, or the run directory itself is not readable.  A
    run that is readable but wrong returns ``passed=False`` instead.
    """


@dataclass(frozen=True)
class ShardCheck:
    """What one independent pass observed about one shard.

    The declared and the observed values are deliberately separate fields.
    Copying ``entry.rows`` into an "actual rows" slot would make a truncated
    shard look intact, which is precisely the failure this class exists to
    expose.

    Attributes:
        part_id: Shard index from the manifest entry.
        path: POSIX path recorded in the manifest entry.
        expected_rows: Row count the manifest declares.
        expected_sha256: Digest the manifest declares.
        observed_rows: Rows actually found, or ``None`` when the archive was
            never parsed (missing file, digest mismatch, unreadable archive).
        observed_sha256: Digest actually computed, or ``None`` when the file
            could not be hashed.
        first_seq: First ``seq`` value actually found, or ``None``.
        last_seq: Last ``seq`` value actually found, or ``None``.
        dtype_ok: ``True``/``False`` once the arrays were inspected, ``None``
            when the digest already failed and the content was never trusted.
        seq_ok: ``True``/``False`` once ``seq`` was inspected, ``None`` when it
            was not checked.
        hash_ok: ``True``/``False`` once the file was hashed, ``None`` when the
            file could not be hashed at all.
        failures: Failure codes for this shard, empty when it is clean.
    """

    # A flat record is the point: nesting would obscure the declared/observed
    # split that the review asked for.
    # pylint: disable=too-many-instance-attributes

    part_id: int
    path: str
    expected_rows: int
    expected_sha256: str
    observed_rows: int | None
    observed_sha256: str | None
    first_seq: int | None
    last_seq: int | None
    dtype_ok: bool | None
    seq_ok: bool | None
    hash_ok: bool | None
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready payload for one shard."""
        return {
            "part_id": self.part_id,
            "path": self.path,
            "expected_rows": self.expected_rows,
            "expected_sha256": self.expected_sha256,
            "observed_rows": self.observed_rows,
            "observed_sha256": self.observed_sha256,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "dtype_ok": self.dtype_ok,
            "seq_ok": self.seq_ok,
            "hash_ok": self.hash_ok,
            "failures": list(self.failures),
        }


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of one :func:`verify_run` call.

    ``elapsed_seconds`` is the independent-verification cost required by
    ``docs/acceptance.md`` section 2.  It is measured inside this call and
    therefore never overlaps the producer's measurement window.

    The aggregate counters describe *verified* data only.  When a shard could
    not be read, ``rows_sum`` covers the shards that could be, and
    ``verified_part_count`` says how many those were; ``rows_expected_sum``
    still reports what the manifest declared.  A caller that wants the run's
    nominal size must read ``total_points``, not ``rows_sum``.

    Attributes:
        run_id: Run identifier from the manifest.
        schema_version: Manifest schema version.
        status: Manifest status as published (``complete`` or ``failed``).
        total_points: Point count the manifest declares.
        rows_expected_sum: Sum of the declared row counts of every entry.
        rows_sum: Sum of the rows actually read, over verified shards only.
        part_count: Number of entries in the manifest.
        verified_part_count: Number of shards whose content was fully read.
        scalar_value_count: ``2 * rows_sum``, one vibration and one temperature
            value per verified sample.
        first_seq: First ``seq`` actually read, or ``None``.
        last_seq: Last ``seq`` actually read, or ``None``.
        shard_shape: Compact description of the verified row pattern.
        shards: Per-shard results in ``part_id`` order.
        failures: Every failure code found, in reporting order.
        elapsed_seconds: Wall-clock cost of this call, in seconds.
        passed: ``True`` only when the run is ``complete`` and nothing failed.
    """

    # pylint: disable=too-many-instance-attributes

    run_id: str
    schema_version: str
    status: str
    total_points: int
    rows_expected_sum: int
    rows_sum: int
    part_count: int
    verified_part_count: int
    scalar_value_count: int
    first_seq: int | None
    last_seq: int | None
    shard_shape: str
    shards: tuple[ShardCheck, ...]
    failures: tuple[str, ...]
    elapsed_seconds: float
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready payload described in section 6 of the issue."""
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "status": self.status,
            "total_points": self.total_points,
            "part_count": self.part_count,
            "verified_part_count": self.verified_part_count,
            "rows_expected_sum": self.rows_expected_sum,
            "rows_sum": self.rows_sum,
            "scalar_value_count": self.scalar_value_count,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "shard_shape": self.shard_shape,
            "elapsed_seconds": self.elapsed_seconds,
            "passed": self.passed,
            "failures": list(self.failures),
            "checks": list(VERIFICATION_CHECKS),
            "shards": [shard.to_dict() for shard in self.shards],
        }


def _require_positive_int(value: object, name: str) -> int:
    """Return ``value`` after refusing anything but a positive integer.

    ``bool`` is an ``int`` subclass, so ``True`` would otherwise pass as ``1``
    and quietly verify a one-point contour.

    Args:
        value: Candidate argument.
        name: Argument name, used in the error message.

    Returns:
        The value as an ``int``.

    Raises:
        ValueError: if it is not an ``int``, is a ``bool``, or is not positive.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _load_manifest(run_dir: Path) -> Manifest:
    """Read and validate the manifest of ``run_dir``.

    Args:
        run_dir: Run directory to read.

    Returns:
        The validated manifest.

    Raises:
        VerificationError: if the directory is not readable, the manifest is
            missing or malformed, or section 5.2 refuses it.
    """
    directory = Path(run_dir)
    if not directory.is_dir():
        raise VerificationError(f"not a run directory: {directory}")
    try:
        return load_manifest_from_run_dir(directory)
    except ConsumptionError as exc:
        raise VerificationError(str(exc)) from exc


def _require_inside_run_dir(run_dir: Path, shard_path: Path, entry: ChunkManifestEntry):
    """Refuse a shard whose real location is not the entry's location.

    This is a path-safety decision, not a data-quality one, so it raises rather
    than becoming a failure code: a manifest entry that resolves outside the run
    directory is either a symlink escape or a hand-edited manifest, and neither
    should be summarised as "one shard was a bit off".

    The rule itself is the public one -- the entry's POSIX path resolved against
    the run directory must land inside that directory at exactly that path --
    and it is applied here rather than inferred from an exception message,
    because the same exception type also reports ordinary missing and corrupt
    shards.

    Args:
        run_dir: Run directory the entry is relative to.
        shard_path: Path the verifier is about to open.
        entry: Manifest entry naming the shard.

    Raises:
        VerificationError: if the resolved path escapes ``run_dir`` or sits at a
            different relative path than the entry records.
    """
    try:
        relative = shard_path.resolve().relative_to(Path(run_dir).resolve())
    except (OSError, ValueError) as exc:
        raise VerificationError(
            f"{shard_path} resolves outside run directory {run_dir}; "
            "a copy that only shares the entry's name is not the published shard"
        ) from exc
    if relative.as_posix() != entry.path:
        raise VerificationError(
            f"{shard_path} is {relative.as_posix()!r} inside the run directory, "
            f"but the manifest records {entry.path!r}"
        )


def _load_arrays(
    shard_path: Path,
) -> tuple[dict[str, np.ndarray[Any, Any]] | None, str]:
    """Load every array of one shard without trusting pickled payloads.

    The container is checked before it is used: ``np.load`` returns a bare
    :class:`numpy.ndarray` when the bytes are a ``.npy`` payload, and an
    ``ndarray`` has no ``files``/context-manager protocol.  Letting that surface
    as a ``TypeError`` would report content damage as a programming error.

    Args:
        shard_path: Shard file.

    Returns:
        A pair ``(arrays, failure)``.  Exactly one element is ``None``.
    """
    try:
        loaded = np.load(shard_path, allow_pickle=False)
    except (OSError, ValueError, EOFError, zipfile.BadZipFile):
        return None, _UNREADABLE

    if not isinstance(loaded, np.lib.npyio.NpzFile):
        # A single-array .npy payload saved under the shard's name.
        if isinstance(loaded, np.ndarray):
            return None, _NOT_ARCHIVE
        return None, _UNREADABLE

    try:
        with loaded as archive:
            arrays = {name: np.asarray(archive[name]) for name in archive.files}
    except (OSError, ValueError, EOFError, zipfile.BadZipFile, TypeError):
        return None, _UNREADABLE
    return arrays, ""


def _check_arrays(
    arrays: dict[str, np.ndarray[Any, Any]], entry: ChunkManifestEntry
) -> tuple[bool, bool, int | None, list[str]]:
    """Compare one shard's arrays against section 5.1.

    Section 5.1 describes four parallel columns of one table, so the four arrays
    must be one-dimensional, carry the frozen dtypes, and be *the same length*.
    Checking only ``seq.shape[0]`` would accept a shard whose vibration column
    lost a row while its digest was recomputed to match, which is exactly the
    damage this module exists to catch.

    A column length only exists for a one-dimensional array.  A 0-d or 2-d
    array has no row axis to compare, so its length is *unknown* rather than
    zero: reading ``shape[0]`` on a 0-d array raises ``IndexError`` and would
    abandon every shard after it.

    Args:
        arrays: Arrays loaded from the shard.
        entry: Manifest entry describing it.

    Returns:
        A tuple ``(dtype_ok, seq_ok, observed_rows, failures)``.
    """
    failures: list[str] = []

    if set(arrays) != set(NPZ_FIELDS):
        failures.append(_FIELDS)
        # The dtype verdict is still meaningful for the fields that are there,
        # but the row count is not, so it stays unknown.
        return False, False, None, failures

    dtype_ok = True
    for name in NPZ_FIELDS:
        array = arrays[name]
        if array.ndim != 1 or array.dtype != FIELD_DTYPES[name]:
            dtype_ok = False
        elif name in ("vibration", "temperature") and not bool(
            np.all(np.isfinite(array))
        ):
            dtype_ok = False
    if not dtype_ok:
        failures.append(_DTYPE)

    # A length is observable only for a 1-d column.  Anything else already
    # failed the dtype check above; comparing a length that does not exist would
    # turn "this column is the wrong shape" into a crash or a bogus mismatch.
    lengths: dict[str, int | None] = {
        name: int(arrays[name].shape[0]) if arrays[name].ndim == 1 else None
        for name in NPZ_FIELDS
    }
    observable = [length for length in lengths.values() if length is not None]
    if len(observable) == len(NPZ_FIELDS) and len(set(observable)) != 1:
        failures.append(_LENGTHS)

    seq = arrays["seq"]
    observed_rows = lengths["seq"]

    if observed_rows is not None and observed_rows != entry.rows:
        failures.append(_ROWS)

    # ``seq`` is only inspected when it is an int64 vector: on any other dtype
    # the values are not a legal column and converting them (NaN -> int) would
    # raise out of a content check.
    seq_ok = True
    if seq.ndim != 1 or seq.dtype != FIELD_DTYPES["seq"] or observed_rows == 0:
        seq_ok = False
    elif not bool(np.all(np.diff(seq) == 1)):
        seq_ok = False
    if not seq_ok:
        failures.append(_SHARD_SEQ)

    return dtype_ok, seq_ok, observed_rows, failures


def _check_shard(run_dir: Path, entry: ChunkManifestEntry) -> ShardCheck:
    """Verify one shard named by ``entry``.

    Args:
        run_dir: Run directory the entry belongs to.
        entry: Manifest entry describing the shard.

    Returns:
        The per-shard result.

    Raises:
        VerificationError: if the entry resolves outside ``run_dir``.
    """
    shard_path = Path(run_dir) / entry.path
    _require_inside_run_dir(run_dir, shard_path, entry)

    observed_sha256: str | None = None
    observed_rows: int | None = None
    first_seq: int | None = None
    last_seq: int | None = None
    dtype_ok: bool | None = None
    seq_ok: bool | None = None
    hash_ok: bool | None = None
    failures: list[str] = []

    if not shard_path.is_file():
        failures.append(_MISSING)
        return ShardCheck(
            entry.part_id,
            entry.path,
            entry.rows,
            entry.sha256,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            tuple(failures),
        )

    try:
        observed_sha256 = sha256_of_file(shard_path)
    except SerializationError:
        failures.append(_UNREADABLE)
        return ShardCheck(
            entry.part_id,
            entry.path,
            entry.rows,
            entry.sha256,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            tuple(failures),
        )

    hash_ok = observed_sha256 == entry.sha256
    if not hash_ok:
        # The digest gate comes first on purpose: content that does not match
        # the published bytes is never parsed, so dtype and seq stay unverified
        # rather than being reported as failures they may not be.
        failures.append(_HASH)
        return ShardCheck(
            entry.part_id,
            entry.path,
            entry.rows,
            entry.sha256,
            None,
            observed_sha256,
            None,
            None,
            None,
            None,
            False,
            tuple(failures),
        )

    arrays, load_failure = _load_arrays(shard_path)
    if arrays is None:
        failures.append(load_failure)
        return ShardCheck(
            entry.part_id,
            entry.path,
            entry.rows,
            entry.sha256,
            None,
            observed_sha256,
            None,
            None,
            None,
            None,
            True,
            tuple(failures),
        )

    dtype_ok, seq_ok, observed_rows, array_failures = _check_arrays(arrays, entry)
    failures.extend(array_failures)
    # The boundary values are only readable when ``seq`` is a legal int64 vector
    # and the shard is not empty.  Converting a NaN/Inf/object column here would
    # raise out of a content check and stop the remaining shards from being
    # inspected, so an unreadable boundary stays unknown instead.
    if seq_ok and observed_rows:
        first_seq = int(arrays["seq"][0])
        last_seq = int(arrays["seq"][-1])

    return ShardCheck(
        entry.part_id,
        entry.path,
        entry.rows,
        entry.sha256,
        observed_rows,
        observed_sha256,
        first_seq,
        last_seq,
        dtype_ok,
        seq_ok,
        True,
        tuple(failures),
    )


def _shard_shape(shards: tuple[ShardCheck, ...]) -> str:
    """Describe the verified row pattern compactly.

    Args:
        shards: Per-shard results in ``part_id`` order.

    Returns:
        A string such as ``10x102400`` or ``9x102400+78400``, built by run-length
        encoding the observed row counts.  Unverified shards are skipped, so an
        empty string means nothing was read.
    """
    groups: list[tuple[int, int]] = []
    for shard in shards:
        if shard.observed_rows is None:
            continue
        if groups and groups[-1][1] == shard.observed_rows:
            groups[-1] = (groups[-1][0] + 1, shard.observed_rows)
        else:
            groups.append((1, shard.observed_rows))
    return "+".join(
        f"{count}x{rows}" if count > 1 else str(rows) for count, rows in groups
    )


def _cross_shard_failures(
    shards: tuple[ShardCheck, ...],
) -> tuple[list[str], int | None]:
    """Check that ``seq`` continues across shard boundaries.

    Args:
        shards: Per-shard results in ``part_id`` order.

    Returns:
        A pair ``(failures, last_seq)`` where ``last_seq`` is the final observed
        ``seq`` or ``None`` when no shard was read.
    """
    failures: list[str] = []
    expected_next: int | None = None
    for shard in shards:
        if shard.first_seq is None or shard.last_seq is None or shard.seq_ok is False:
            continue
        if expected_next is None:
            # Only ``part_id == 0`` may be judged against the origin.  When the
            # first shard itself is unreadable, the next readable shard becomes
            # the baseline of the chain; flagging it as "not from zero" would
            # blame a healthy shard for a neighbour's damage.
            if shard.part_id == 0 and shard.first_seq != 0:
                failures.append(_SEQ_ORIGIN)
        elif shard.first_seq != expected_next:
            failures.append(_SEQ_CROSS)
        expected_next = shard.last_seq + 1
    return failures, (expected_next - 1 if expected_next is not None else None)


def verify_run(
    run_dir: Path,
    *,
    expected_total_points: int | None = None,
) -> VerificationResult:
    """Verify a published run against ``docs/interface.md`` section 5.2.

    Reads only ``run_dir``: the manifest and the shards it names.  It never
    opens the SQLite database, never reads ``private_truth/``, and never writes
    anything.

    Args:
        run_dir: The run directory to verify.
        expected_total_points: Optional guard for acceptance runs.  When given
            and the manifest declares a different ``total_points``, the run is
            reported as failed with ``rows.expected_mismatch`` rather than
            silently verified against the wrong contour.

    Returns:
        A :class:`VerificationResult`.  ``passed`` is ``True`` only when the run
        is ``complete`` and ``failures`` is empty.

    Raises:
        ValueError: if ``expected_total_points`` is given but is not a positive
            integer.  A bad argument is a programming error, not a verdict about
            the run, so it is kept separate from ``rows.expected_mismatch``.
        VerificationError: if the manifest is missing or structurally invalid,
            an entry path escapes ``run_dir``, or the run directory cannot be
            read.
    """
    started = time.perf_counter()

    if expected_total_points is not None:
        expected_total_points = _require_positive_int(
            expected_total_points, "expected_total_points"
        )

    manifest = _load_manifest(Path(run_dir))
    entries = manifest.parts
    shards = tuple(_check_shard(Path(run_dir), entry) for entry in entries)

    failures: list[str] = []
    if manifest.status is not ManifestStatus.COMPLETE:
        failures.append(_NOT_COMPLETE)
    if (
        expected_total_points is not None
        and manifest.total_points != expected_total_points
    ):
        failures.append(_EXPECTED)

    cross_failures, last_seq = _cross_shard_failures(shards)
    failures.extend(cross_failures)
    for shard in shards:
        failures.extend(shard.failures)

    # Only shards whose four columns were read, carry the frozen dtypes and
    # agree in length count as verified tables.  A shard whose vibration column
    # lost a row still has a readable ``seq``, but claiming ``2 x rows`` scalars
    # were verified would repeat exactly the mistake section 5.1 forbids.
    verified = tuple(
        shard
        for shard in shards
        if shard.observed_rows is not None
        and _LENGTHS not in shard.failures
        and _DTYPE not in shard.failures
    )
    rows_sum = sum(shard.observed_rows or 0 for shard in verified)
    first_seq = verified[0].first_seq if verified else None

    elapsed = time.perf_counter() - started
    passed = not failures

    return VerificationResult(
        run_id=manifest.run_id,
        schema_version=manifest.schema_version,
        status=manifest.status.value,
        total_points=manifest.total_points,
        rows_expected_sum=sum(entry.rows for entry in entries),
        rows_sum=rows_sum,
        part_count=len(entries),
        verified_part_count=len(verified),
        scalar_value_count=rows_sum * 2,
        first_seq=first_seq,
        last_seq=last_seq,
        shard_shape=_shard_shape(shards),
        shards=shards,
        failures=tuple(failures),
        elapsed_seconds=elapsed,
        passed=passed,
    )
