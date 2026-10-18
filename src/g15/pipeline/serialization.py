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
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final

import numpy as np

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

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
    from g15.simulation.engine import SimulationChunk

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
    On any failure the temporary file is removed and the destination path is
    left untouched, which keeps a failed run from masquerading as a complete one.

    Args:
        chunk: Shard to persist; validated against
            :func:`~g15.pipeline.schema.validate_chunk`.
        output_path: Destination file, conventionally
            ``<run_dir>/raw/part-000000.npz``.
        run_dir: Run directory used to derive the manifest-relative path.  When
            omitted, the entry records the bare file name, which is still
            relative and therefore safe.
        part_id: Shard index.  When omitted it is parsed from the file name; a
            non-canonical name then raises instead of guessing.

    Returns:
        The entry describing the published shard.

    Raises:
        ChunkValidationError: if the chunk violates section 5.1.
        ChunkIOError: if the path is unusable, the name carries no ``part_id``,
            ``run_dir`` does not contain the shard, or the write fails.
    """
    validate_chunk(chunk)

    path = Path(output_path)
    if part_id is None:
        part_id = _part_id_from_name(path.name)
        if part_id is None:
            raise ChunkIOError(
                f"cannot derive part_id from {path.name!r}; name the shard "
                "part-<digits>.npz or pass part_id explicitly"
            )

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
        os.replace(tmp_path, path)
    except OSError as exc:
        _remove_quietly(tmp_path)
        raise ChunkIOError(f"cannot write shard {path}: {exc}") from exc
    except BaseException:
        _remove_quietly(tmp_path)
        raise

    return ChunkManifestEntry(
        part_id=part_id,
        path=_relative_entry_path(path, run_dir),
        rows=int(np.asarray(chunk.seq).shape[0]),
        sha256=digest,
    )


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
    except (OSError, ValueError, EOFError) as exc:
        raise ChunkIOError(f"cannot read shard {path}: {exc}") from exc


def _build_chunk(arrays: dict[str, np.ndarray[Any, Any]]) -> "SimulationChunk":
    """Construct a ``SimulationChunk`` from loaded arrays.

    The type is imported lazily: it is defined in :mod:`g15.simulation.engine`
    today and Issue #17 may move it to :mod:`g15.common.types`.  A local import
    keeps that migration to a single line and avoids a module-level dependency
    from ``pipeline`` on ``simulation``.

    Args:
        arrays: The four frozen arrays.

    Returns:
        A chunk ready to hand to the consumer or the analysis layer.
    """
    # Local import keeps the Issue #17 migration to one line.
    # pylint: disable=import-outside-toplevel
    from g15.simulation.engine import SimulationChunk

    return SimulationChunk(
        seq=arrays["seq"],
        timestamp_ns=arrays["timestamp_ns"],
        vibration=arrays["vibration"],
        temperature=arrays["temperature"],
    )


def read_chunk(path: Path, manifest_entry: ChunkManifestEntry) -> "SimulationChunk":
    """Load and verify one shard described by ``manifest_entry``.

    Verification order is deliberate: existence, entry safety, file identity,
    digest, then content.  The digest is checked before the arrays are trusted,
    so a corrupted or swapped file never reaches the analysis layer.

    Args:
        path: Location of the shard on disk, normally
            ``run_dir / manifest_entry.path``.
        manifest_entry: Entry from a published manifest.

    Returns:
        The verified chunk.

    Raises:
        ManifestError: if ``manifest_entry.path`` is not a safe relative path.
        ChunkIOError: if the file is missing, is not the file the entry
            describes, or cannot be parsed.
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
