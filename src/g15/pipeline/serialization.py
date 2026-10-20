"""Atomic, checksummed I/O for the G15 NPZ shards.

This is the only module that touches shard files, so every durability and
integrity rule of ``docs/interface.md`` section 5.1 lives here:

1. **Write to a temporary file in the same directory, then rename.**  A reader
   either sees the finished shard or nothing at all; ``os.replace`` is atomic on
   a POSIX filesystem and on Windows.
2. **Hand an open binary handle to** :func:`numpy.savez`.  With a *path* NumPy
   appends ``.npz`` on its own, which would leave ``part-000000.npz.tmp.npz``
   behind and break the rename.
3. **Hash after the handle is closed.**  Reading the temporary file back in
   chunks is both simpler and more trustworthy than wrapping ``savez`` in a
   hashing stream, because it hashes exactly the bytes that are about to be
   published.
4. **Never return half-trusted data.**  :func:`read_chunk` refuses a missing
   file, a wrong digest, a wrong field set, a wrong dtype, a wrong row count,
   a discontinuous ``seq`` and a non-finite signal, raising instead of warning.
   Given the run directory it also refuses a shard that only *looks* like the
   published one: a byte-identical copy outside the run, or a symlink leading
   out of it, fails the ownership check.
5. **Validate before publishing.**  Every path rule and every ``part_id`` rule is
   checked *before* the temporary file is created, and the manifest entry --
   whose digest is only known after hashing -- is built and validated *before*
   ``os.replace``.  A rejected call therefore leaves an existing shard
   byte-for-byte unchanged and never creates the file it refuses.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Final

import numpy as np

from g15.common.types import SimulationChunk

from g15.pipeline.schema import (
    CHUNK_FILE_PATTERN,
    NPZ_FIELDS,
    ChunkManifestEntry,
    ChunkValidationError,
    DataContractError,
    ManifestError,
    is_safe_relative_path,
    validate_chunk,
)

__all__ = [
    "ChecksumMismatchError",
    "ChunkIOError",
    "SerializationError",
    "read_chunk",
    "sha256_of_file",
    "write_chunk_atomic",
]

#: Size of the buffer used when hashing a shard, in bytes.
_HASH_CHUNK_SIZE: Final[int] = 1024 * 1024

#: Suffix of the temporary file a shard is written to before being renamed.
_TMP_SUFFIX: Final[str] = ".tmp"


class SerializationError(DataContractError):
    """Base class for shard I/O failures.

    Inherits from :class:`~g15.pipeline.schema.DataContractError` so the CLI can
    map every contract problem to a single exit code (``3``).
    """


class ChunkIOError(SerializationError):
    """A shard cannot be written, found, parsed, or matched to its entry."""


class ChecksumMismatchError(SerializationError):
    """A shard's SHA-256 does not match the digest recorded in the manifest."""


def sha256_of_file(path: Path) -> str:
    """Return the lower-case hexadecimal SHA-256 of ``path``.

    The file is read in bounded chunks so that hashing a 32 MB shard does not
    cost a second full-size allocation.

    Args:
        path: File to hash.

    Returns:
        A 64-character lower-case hexadecimal digest.

    Raises:
        ChunkIOError: if the file cannot be read.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
                digest.update(block)
    except OSError as exc:
        raise ChunkIOError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _part_id_from_name(name: str) -> int | None:
    """Return the ``part_id`` encoded in a canonical shard file name.

    Args:
        name: File name such as ``part-000007.npz``.

    Returns:
        The numeric group, or ``None`` when the name is not canonical.
    """
    match = CHUNK_FILE_PATTERN.match(name)
    return int(match.group(1)) if match else None


def _relative_entry_path(output_path: Path, run_dir: Path | None) -> str:
    """Return the POSIX path recorded in the manifest entry.

    Args:
        output_path: Absolute or relative location the shard was written to.
        run_dir: Run directory the path must be relative to; ``None`` keeps only
            the file name.

    Returns:
        A POSIX path relative to ``run_dir``, or the bare file name.

    Raises:
        ChunkIOError: if ``run_dir`` was given but does not contain
            ``output_path``.
    """
    if run_dir is None:
        return output_path.name
    try:
        relative = output_path.resolve().relative_to(run_dir.resolve())
    except ValueError as exc:
        raise ChunkIOError(
            f"{output_path} is not inside run directory {run_dir}"
        ) from exc
    return relative.as_posix()


def _resolve_part_id(path: Path, part_id: int | None) -> int:
    """Return the shard index, refusing any index the file name contradicts.

    The check runs before the shard is written.  Skipping it would let a caller
    overwrite ``part-000000.npz`` while the returned entry claims
    ``part_id=1``, which publishes a shard whose name and manifest entry
    disagree.

    Args:
        path: Destination file.
        part_id: Explicit index, or ``None`` to parse it from the file name.

    Returns:
        The validated, non-negative shard index.

    Raises:
        ChunkIOError: if the name is not canonical and no index was given, if
            ``part_id`` is not a non-negative integer, or if it disagrees with
            the index encoded in a canonical name.
    """
    from_name = _part_id_from_name(path.name)
    if part_id is None:
        if from_name is None:
            raise ChunkIOError(
                f"cannot derive part_id from {path.name!r}; name the shard "
                "part-<digits>.npz or pass part_id explicitly"
            )
        return from_name
    if not isinstance(part_id, int) or isinstance(part_id, bool):
        raise ChunkIOError(f"part_id must be an integer, got {part_id!r}")
    if part_id < 0:
        raise ChunkIOError(f"part_id must be non-negative, got {part_id}")
    if from_name is not None and from_name != part_id:
        raise ChunkIOError(
            f"{path.name!r} encodes part_id {from_name}, but part_id={part_id} "
            "was passed; the manifest entry would not describe the file name"
        )
    return part_id


def _fsync(handle: Any) -> None:
    """Flush ``handle`` to the operating system and force it to the device.

    Args:
        handle: Writable binary file object.

    Raises:
        ChunkIOError: if flushing fails.
    """
    try:
        handle.flush()
        os.fsync(handle.fileno())
    except OSError as exc:  # pragma: no cover - platform dependent
        raise ChunkIOError(f"cannot flush shard data: {exc}") from exc


def write_chunk_atomic(
    chunk: "SimulationChunk",
    output_path: Path,
    *,
    run_dir: Path | None = None,
    part_id: int | None = None,
) -> ChunkManifestEntry:
    """Write one shard atomically and return its manifest entry.

    The chunk is validated first, so an invalid shard never reaches the disk.
    The path rules and the shard index are checked next, and the manifest entry
    is built and validated before the rename, so *every* rejection leaves the
    destination exactly as it was: an existing shard keeps its bytes and a new
    path is never created.  On any failure the temporary file is removed.

    Args:
        chunk: Shard to persist; validated against
            :func:`~g15.pipeline.schema.validate_chunk`.
        output_path: Destination file, conventionally
            ``<run_dir>/raw/part-000000.npz``.
        run_dir: Run directory used to derive the manifest-relative path.  When
            omitted, the entry records the bare file name, which is still
            relative and therefore safe.
        part_id: Shard index.  When omitted it is parsed from the file name; a
            non-canonical name then raises instead of guessing.  When given it
            must agree with the index a canonical name carries.

    Returns:
        The entry describing the published shard.

    Raises:
        ChunkValidationError: if the chunk violates section 5.1.
        ChunkIOError: if the path is unusable, the name carries no ``part_id``,
            ``part_id`` contradicts the name, ``run_dir`` does not contain the
            shard, or the write fails.
        ManifestError: if the entry cannot be built; raised before publication,
            so it never leaves a shard behind.
    """
    validate_chunk(chunk)

    path = Path(output_path)
    resolved_part_id = _resolve_part_id(path, part_id)
    relative_path = _relative_entry_path(path, run_dir)
    rows = int(np.asarray(chunk.seq).shape[0])

    tmp_path = path.with_name(path.name + _TMP_SUFFIX)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "wb") as handle:
            np.savez(
                handle,
                seq=np.asarray(chunk.seq),
                timestamp_ns=np.asarray(chunk.timestamp_ns),
                vibration=np.asarray(chunk.vibration),
                temperature=np.asarray(chunk.temperature),
            )
            _fsync(handle)
        digest = sha256_of_file(tmp_path)
        # The digest is the last unknown field, so the entry is built -- and
        # therefore validated -- here, still before the irreversible rename.
        entry = ChunkManifestEntry(
            part_id=resolved_part_id,
            path=relative_path,
            rows=rows,
            sha256=digest,
        )
        os.replace(tmp_path, path)
    except OSError as exc:
        _remove_quietly(tmp_path)
        raise ChunkIOError(f"cannot write shard {path}: {exc}") from exc
    except BaseException:
        _remove_quietly(tmp_path)
        raise

    return entry


def _remove_quietly(path: Path) -> None:
    """Delete ``path`` if it exists, ignoring a concurrent removal.

    Args:
        path: Temporary file to clean up.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:  # pragma: no cover - defensive
        return


def _load_npz(path: Path) -> dict[str, np.ndarray[Any, Any]]:
    """Load every array of a shard, refusing pickled payloads.

    Args:
        path: Shard file.

    Returns:
        The arrays keyed by field name, materialised in memory so the caller can
        close the file.

    Raises:
        ChunkIOError: if the file is missing or is not a readable NPZ archive.
        ChunkValidationError: if the field set is not exactly
            :data:`~g15.pipeline.schema.NPZ_FIELDS`.
    """
    if not path.is_file():
        raise ChunkIOError(f"shard does not exist: {path}")
    try:
        with np.load(path, allow_pickle=False) as archive:
            fields = set(archive.files)
            expected = set(NPZ_FIELDS)
            if fields != expected:
                raise ChunkValidationError(
                    f"{path.name} has the wrong field set; missing="
                    f"{sorted(expected - fields)}, unknown="
                    f"{sorted(fields - expected)}"
                )
            return {name: np.asarray(archive[name]) for name in NPZ_FIELDS}
    except (OSError, ValueError, EOFError, zipfile.BadZipFile) as exc:
        # zipfile.BadZipFile inherits Exception, *not* OSError: a shard whose
        # digest matches but whose bytes are not a zip would otherwise escape
        # the contract family and reach the CLI as a traceback instead of
        # exit 3 (docs/interface.md section 12, "corrupted file isolation").
        raise ChunkIOError(f"cannot read shard {path}: {exc}") from exc


def _build_chunk(arrays: dict[str, np.ndarray[Any, Any]]) -> "SimulationChunk":
    """Construct a ``SimulationChunk`` from loaded arrays.

    Issue #17 settled the ownership question: the single public definition now
    lives in :mod:`g15.common.types`, and section 2 of ``docs/interface.md``
    allows the ``pipeline -> common`` edge, so the import is no longer local.

    Args:
        arrays: The four frozen arrays.

    Returns:
        A chunk ready to hand to the consumer or the analysis layer.
    """
    return SimulationChunk(
        seq=arrays["seq"],
        timestamp_ns=arrays["timestamp_ns"],
        vibration=arrays["vibration"],
        temperature=arrays["temperature"],
    )


def _require_inside_run_dir(
    shard_path: Path, manifest_entry: ChunkManifestEntry, run_dir: Path
) -> None:
    """Refuse a shard whose real location is not the entry's location in the run.

    A manifest entry names one file relative to one run directory.  Checking the
    bare file name and the digest is not an ownership check: a byte-identical
    copy of ``raw/part-000000.npz`` parked anywhere on disk would pass both, and
    a consumer that trusts it reads data the run never published.  Resolving both
    sides also rejects a symlink that tries to escape the run directory, because
    ``resolve`` follows the link before the containment test.

    Args:
        shard_path: Path the caller is about to read.
        manifest_entry: Entry describing the intended file.
        run_dir: Run directory the entry is relative to.

    Raises:
        ChunkIOError: if the resolved shard is outside ``run_dir``, or if it is
            inside but at a different relative path than the entry records.
    """
    try:
        relative = shard_path.resolve().relative_to(Path(run_dir).resolve())
    except ValueError as exc:
        raise ChunkIOError(
            f"{shard_path} is outside run directory {run_dir}; a copy that only "
            "shares the entry's name and digest is not the published shard"
        ) from exc
    if relative.as_posix() != manifest_entry.path:
        raise ChunkIOError(
            f"{shard_path} is {relative.as_posix()!r} inside the run directory, "
            f"but the manifest records {manifest_entry.path!r}"
        )


def read_chunk(
    path: Path,
    manifest_entry: ChunkManifestEntry,
    *,
    run_dir: Path | None = None,
) -> "SimulationChunk":
    """Load and verify one shard described by ``manifest_entry``.

    Verification order is deliberate: existence, entry safety, file identity,
    digest, then content.  The digest is checked before the arrays are trusted,
    so a corrupted or swapped file never reaches the analysis layer.

    Args:
        path: Location of the shard on disk, normally
            ``run_dir / manifest_entry.path``.
        manifest_entry: Entry from a published manifest.
        run_dir: Run directory the entry belongs to.  When given, the resolved
            shard must sit exactly at ``run_dir / manifest_entry.path``, which
            turns the name-and-digest match into a real ownership check and
            blocks symlink escapes.  When omitted the function performs only
            file-identity, digest and field validation, so the *consumer* entry
            point that knows the run directory must pass it.

    Returns:
        The verified chunk.

    Raises:
        ManifestError: if ``manifest_entry.path`` is not a safe relative path.
        ChunkIOError: if the file is missing, is not the file the entry
            describes, lies outside ``run_dir``, or cannot be parsed.
        ChecksumMismatchError: if the digest differs from the recorded one.
        ChunkValidationError: if the content violates section 5.1 or the row
            count differs from the entry.
    """
    if not is_safe_relative_path(manifest_entry.path):
        raise ManifestError(f"refusing unsafe manifest path {manifest_entry.path!r}")

    shard_path = Path(path)
    if shard_path.name != PurePosixPath(manifest_entry.path).name:
        raise ChunkIOError(
            f"{shard_path.name!r} does not match manifest entry "
            f"{manifest_entry.path!r}"
        )
    if run_dir is not None:
        _require_inside_run_dir(shard_path, manifest_entry, run_dir)
    if not shard_path.is_file():
        raise ChunkIOError(f"shard does not exist: {shard_path}")

    actual_digest = sha256_of_file(shard_path)
    if actual_digest != manifest_entry.sha256:
        raise ChecksumMismatchError(
            f"{shard_path.name}: sha256 is {actual_digest}, manifest records "
            f"{manifest_entry.sha256}"
        )

    arrays = _load_npz(shard_path)
    chunk = _build_chunk(arrays)
    validate_chunk(chunk)

    rows = int(arrays["seq"].shape[0])
    if rows != manifest_entry.rows:
        raise ChunkValidationError(
            f"{shard_path.name} holds {rows} rows, manifest records "
            f"{manifest_entry.rows}"
        )
    return chunk
