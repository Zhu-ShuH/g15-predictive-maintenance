"""Unit tests for the NPZ shard and manifest contract (Issue #3, owner C4).

Every assertion here is falsifiable: the corrupted, mis-typed and truncated
cases really are built on disk and really are read back, so a regression in
:mod:`g15.pipeline.serialization` turns the suite red instead of silently
degrading what a consumer is allowed to trust.

The fixtures write into ``tmp_path`` and never into ``runs/``, so the suite
leaves no artefacts behind and does not depend on ``configs/*.yaml`` (Issue #4
is still open).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from g15.pipeline.schema import (
    NPZ_FIELDS,
    SCHEMA_VERSION,
    ChunkManifestEntry,
    ChunkValidationError,
    Manifest,
    ManifestError,
    ManifestStatus,
    is_safe_relative_path,
    manifest_from_dict,
    manifest_to_dict,
    validate_chunk,
)
from g15.pipeline.serialization import (
    ChecksumMismatchError,
    ChunkIOError,
    read_chunk,
    sha256_of_file,
    write_chunk_atomic,
)
from g15.simulation.engine import SimulationChunk

# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #

START_TIME_UTC = "2026-10-14T00:00:00Z"


def make_chunk(
    count: int = 8,
    start_seq: int = 0,
    sample_rate_hz: int = 100,
) -> SimulationChunk:
    """Build a small, contract-compliant chunk without touching the simulator."""
    seq = np.arange(start_seq, start_seq + count, dtype=np.int64)
    timestamp = np.int64(1_700_000_000) * 1_000_000_000 + (
        seq * (1_000_000_000 // sample_rate_hz)
    )
    return SimulationChunk(
        seq=seq,
        timestamp_ns=timestamp.astype(np.int64),
        vibration=np.linspace(0.5, 0.6, count, dtype=np.float64),
        temperature=np.linspace(35.0, 35.1, count, dtype=np.float64),
    )


def shard_path(tmp_path: Path, part_id: int = 0) -> Path:
    """Return the conventional location of one shard inside ``tmp_path``."""
    return tmp_path / "raw" / f"part-{part_id:06d}.npz"


def write_raw(path: Path, **arrays: np.ndarray) -> None:
    """Write an NPZ archive bypassing the producer-side validation.

    Used to build shards that are well-formed zip files but violate the data
    contract, so the reader is exercised rather than the writer.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        np.savez(handle, **arrays)


def entry_for(
    path: Path,
    rows: int,
    part_id: int = 0,
    manifest_path: str | None = None,
) -> ChunkManifestEntry:
    """Build a manifest entry whose digest matches ``path`` exactly."""
    return ChunkManifestEntry(
        part_id=part_id,
        path=manifest_path if manifest_path is not None else f"raw/{path.name}",
        rows=rows,
        sha256=sha256_of_file(path),
    )


def contract_arrays(count: int = 8, start_seq: int = 0) -> dict[str, np.ndarray]:
    """Return the four frozen arrays as plain NumPy values."""
    chunk = make_chunk(count, start_seq)
    return {
        "seq": chunk.seq,
        "timestamp_ns": chunk.timestamp_ns,
        "vibration": chunk.vibration,
        "temperature": chunk.temperature,
    }


def make_manifest(
    parts: tuple[ChunkManifestEntry, ...],
    total_points: int,
    status: ManifestStatus = ManifestStatus.COMPLETE,
) -> Manifest:
    """Build a manifest around ``parts`` with the frozen metadata."""
    return Manifest(
        schema_version=SCHEMA_VERSION,
        run_id="20261014T000000Z-bearing_001-a1b2c3",
        scenario_id="bearing_001",
        device_id="DEV-001",
        seed=42,
        sample_rate_hz=100,
        start_time_utc=START_TIME_UTC,
        total_points=total_points,
        units={"vibration": "g", "temperature": "degC"},
        status=status,
        parts=parts,
    )


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #


def test_round_trip_preserves_every_array(tmp_path: Path) -> None:
    """All four arrays must come back element-for-element identical."""
    chunk = make_chunk(count=1024)
    path = shard_path(tmp_path)

    entry = write_chunk_atomic(chunk, path, run_dir=tmp_path)
    restored = read_chunk(path, entry)

    assert entry.rows == 1024
    assert entry.part_id == 0
    assert entry.path == "raw/part-000000.npz"
    assert len(entry.sha256) == 64
    for name in NPZ_FIELDS:
        assert np.array_equal(getattr(restored, name), getattr(chunk, name))
        assert getattr(restored, name).dtype == getattr(chunk, name).dtype


def test_entry_path_is_relative_to_run_dir(tmp_path: Path) -> None:
    """A shard written under ``run_dir`` must be recorded by relative path."""
    chunk = make_chunk()

    entry = write_chunk_atomic(chunk, shard_path(tmp_path, part_id=3), run_dir=tmp_path)

    assert entry.path == "raw/part-000003.npz"
    assert entry.part_id == 3
    assert is_safe_relative_path(entry.path)
    assert not Path(entry.path).is_absolute()


def test_sha256_matches_the_published_bytes(tmp_path: Path) -> None:
    """The digest in the entry must equal the digest of the final file."""
    chunk = make_chunk()
    path = shard_path(tmp_path)

    entry = write_chunk_atomic(chunk, path, run_dir=tmp_path)

    assert entry.sha256 == sha256_of_file(path)


def test_tail_shard_shorter_than_chunk_points(tmp_path: Path) -> None:
    """The last shard of a 1M run is allowed to be short (78400 rows)."""
    chunk = make_chunk(count=78_400, start_seq=921_600)
    path = shard_path(tmp_path, part_id=9)

    entry = write_chunk_atomic(chunk, path, run_dir=tmp_path)
    restored = read_chunk(path, entry)

    assert entry.rows == 78_400
    assert int(restored.seq[-1]) == 999_999


def test_empty_chunk_is_refused_on_write(tmp_path: Path) -> None:
    """An empty shard has no meaning in the contract and must not be written."""
    path = shard_path(tmp_path)

    with pytest.raises(ChunkValidationError, match="empty shard"):
        write_chunk_atomic(make_chunk(count=0), path, run_dir=tmp_path)

    assert not path.exists()


def test_non_canonical_name_needs_explicit_part_id(tmp_path: Path) -> None:
    """Without a ``part-NNNNNN.npz`` name the writer refuses to guess."""
    with pytest.raises(ChunkIOError, match="cannot derive part_id"):
        write_chunk_atomic(make_chunk(), tmp_path / "chunk.npz", run_dir=tmp_path)

    assert not (tmp_path / "chunk.npz").exists()


# --------------------------------------------------------------------------- #
# Rejection on read
# --------------------------------------------------------------------------- #


def test_corrupted_byte_is_refused(tmp_path: Path) -> None:
    """Flipping one byte must break the digest and stop the read."""
    path = shard_path(tmp_path)
    entry = write_chunk_atomic(make_chunk(), path, run_dir=tmp_path)

    payload = bytearray(path.read_bytes())
    payload[-1] ^= 0xFF
    path.write_bytes(bytes(payload))

    with pytest.raises(ChecksumMismatchError, match="sha256"):
        read_chunk(path, entry)


def test_missing_field_is_refused(tmp_path: Path) -> None:
    """A shard without ``vibration`` violates section 5.1."""
    arrays = contract_arrays()
    del arrays["vibration"]
    path = shard_path(tmp_path)
    write_raw(path, **arrays)

    with pytest.raises(ChunkValidationError, match="missing=\\['vibration'\\]"):
        read_chunk(path, entry_for(path, rows=8))


def test_extra_field_is_refused(tmp_path: Path) -> None:
    """A fifth array is a contract change, not an extension."""
    arrays = contract_arrays()
    arrays["run_id"] = np.array([1, 2, 3], dtype=np.int64)
    path = shard_path(tmp_path)
    write_raw(path, **arrays)

    with pytest.raises(ChunkValidationError, match="unknown=\\['run_id'\\]"):
        read_chunk(path, entry_for(path, rows=8))


def test_wrong_dtype_is_refused(tmp_path: Path) -> None:
    """``vibration`` must be float64; float32 is a silent-precision bug."""
    arrays = contract_arrays()
    arrays["vibration"] = arrays["vibration"].astype(np.float32)
    path = shard_path(tmp_path)
    write_raw(path, **arrays)

    with pytest.raises(ChunkValidationError, match="vibration must be"):
        read_chunk(path, entry_for(path, rows=8))


def test_unequal_lengths_are_refused(tmp_path: Path) -> None:
    """The four arrays must be equally long."""
    arrays = contract_arrays()
    arrays["temperature"] = arrays["temperature"][:-1]
    path = shard_path(tmp_path)
    write_raw(path, **arrays)

    with pytest.raises(ChunkValidationError, match="equally long"):
        read_chunk(path, entry_for(path, rows=8))


def test_discontinuous_seq_is_refused(tmp_path: Path) -> None:
    """A gap in ``seq`` means lost samples, not a shorter shard."""
    arrays = contract_arrays()
    arrays["seq"] = arrays["seq"].copy()
    arrays["seq"][4] = 99
    path = shard_path(tmp_path)
    write_raw(path, **arrays)

    with pytest.raises(ChunkValidationError, match="contiguous"):
        read_chunk(path, entry_for(path, rows=8))


def test_nan_and_inf_are_refused(tmp_path: Path) -> None:
    """Non-finite samples must never reach the analysis layer."""
    for bad in (np.nan, np.inf, -np.inf):
        arrays = contract_arrays()
        arrays["vibration"] = arrays["vibration"].copy()
        arrays["vibration"][2] = bad
        path = shard_path(tmp_path)
        write_raw(path, **arrays)

        with pytest.raises(ChunkValidationError, match="not finite"):
            read_chunk(path, entry_for(path, rows=8))


def test_row_count_mismatch_is_refused(tmp_path: Path) -> None:
    """The entry's ``rows`` must match what the file actually holds."""
    path = shard_path(tmp_path)
    entry = write_chunk_atomic(make_chunk(), path, run_dir=tmp_path)
    wrong = ChunkManifestEntry(
        part_id=entry.part_id, path=entry.path, rows=7, sha256=entry.sha256
    )

    with pytest.raises(ChunkValidationError, match="manifest records 7"):
        read_chunk(path, wrong)


def test_missing_file_is_refused(tmp_path: Path) -> None:
    """A manifest that points at nothing must fail loudly."""
    entry = ChunkManifestEntry(
        part_id=0, path="raw/part-000000.npz", rows=8, sha256="0" * 64
    )

    with pytest.raises(ChunkIOError, match="does not exist"):
        read_chunk(tmp_path / "raw" / "part-000000.npz", entry)


def test_entry_pointing_at_another_file_is_refused(tmp_path: Path) -> None:
    """A valid shard read through the wrong entry must not be trusted."""
    path = shard_path(tmp_path, part_id=0)
    entry = write_chunk_atomic(make_chunk(), path, run_dir=tmp_path)
    other = shard_path(tmp_path, part_id=1)

    with pytest.raises(ChunkIOError, match="does not match manifest entry"):
        read_chunk(other, entry)


def test_unsafe_manifest_path_is_refused(tmp_path: Path) -> None:
    """Absolute, parent-relative and Windows paths are all rejected."""
    path = shard_path(tmp_path)
    write_chunk_atomic(make_chunk(), path, run_dir=tmp_path)

    for unsafe in ("/etc/passwd", "../outside.npz", "raw/../../x.npz", "raw\\x.npz"):
        with pytest.raises(ManifestError, match="relative POSIX path"):
            ChunkManifestEntry(part_id=0, path=unsafe, rows=8, sha256="0" * 64)


# --------------------------------------------------------------------------- #
# Atomicity
# --------------------------------------------------------------------------- #


def test_failed_write_leaves_no_temporary_file(tmp_path: Path, monkeypatch) -> None:
    """A failure must not litter the directory with ``.tmp`` or ``.tmp.npz``."""
    path = shard_path(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated savez failure")

    monkeypatch.setattr("g15.pipeline.serialization.np.savez", boom)

    with pytest.raises(RuntimeError, match="simulated"):
        write_chunk_atomic(make_chunk(), path, run_dir=tmp_path)

    assert not path.exists()
    leftovers = sorted(item.name for item in path.parent.iterdir())
    assert leftovers == []
    assert not any(name.endswith(".tmp.npz") for name in leftovers)


def test_successful_write_leaves_exactly_one_file(tmp_path: Path) -> None:
    """The rename must consume the temporary file."""
    path = shard_path(tmp_path)

    write_chunk_atomic(make_chunk(), path, run_dir=tmp_path)

    assert [item.name for item in path.parent.iterdir()] == ["part-000000.npz"]
    with np.load(path, allow_pickle=False) as archive:
        assert set(archive.files) == set(NPZ_FIELDS)


def test_overwriting_a_shard_replaces_it_atomically(tmp_path: Path) -> None:
    """Rewriting the same part must publish the new digest, not a mix."""
    path = shard_path(tmp_path)
    first = write_chunk_atomic(make_chunk(count=8), path, run_dir=tmp_path)
    second = write_chunk_atomic(make_chunk(count=16), path, run_dir=tmp_path)

    assert first.sha256 != second.sha256
    assert second.rows == 16
    assert read_chunk(path, second).seq.shape[0] == 16


# --------------------------------------------------------------------------- #
# validate_chunk and path rules
# --------------------------------------------------------------------------- #


def test_validate_chunk_accepts_a_contract_compliant_shard() -> None:
    """The happy path must not raise."""
    validate_chunk(make_chunk(count=32, start_seq=512))


def test_validate_chunk_rejects_a_missing_field() -> None:
    """Duck-typed input is checked for the four documented attributes."""

    class Partial:  # pylint: disable=too-few-public-methods
        seq = np.arange(4, dtype=np.int64)
        timestamp_ns = np.arange(4, dtype=np.int64)

    with pytest.raises(ChunkValidationError, match="no field 'vibration'"):
        validate_chunk(Partial)


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("raw/part-000000.npz", True),
        ("manifest.json", True),
        ("", False),
        ("/abs/part.npz", False),
        ("C:/runs/part.npz", False),
        ("raw/../part.npz", False),
        ("raw\\part.npz", False),
        ("raw//part.npz", False),
        (None, False),
        (7, False),
    ],
)
def test_is_safe_relative_path(candidate: object, expected: bool) -> None:
    """Only plain relative POSIX paths may appear in a manifest."""
    assert is_safe_relative_path(candidate) is expected


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def test_manifest_round_trip_through_json(tmp_path: Path) -> None:
    """The manifest must survive a JSON round trip unchanged."""
    entries = tuple(
        write_chunk_atomic(
            make_chunk(count=10, start_seq=index * 10),
            shard_path(tmp_path, part_id=index),
            run_dir=tmp_path,
        )
        for index in range(3)
    )
    manifest = make_manifest(entries, total_points=30)

    payload = json.loads(json.dumps(manifest_to_dict(manifest)))
    restored = manifest_from_dict(payload)

    assert restored == manifest
    assert set(payload) == {
        "schema_version",
        "run_id",
        "scenario_id",
        "device_id",
        "seed",
        "sample_rate_hz",
        "start_time_utc",
        "total_points",
        "units",
        "status",
        "parts",
    }
    assert payload["status"] == "complete"
    assert [part["path"] for part in payload["parts"]] == [
        "raw/part-000000.npz",
        "raw/part-000001.npz",
        "raw/part-000002.npz",
    ]


def test_manifest_rejects_an_unknown_field(tmp_path: Path) -> None:
    """An unknown key is refused instead of silently ignored."""
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    payload = manifest_to_dict(make_manifest((entry,), total_points=8))
    payload["comment"] = "looks fine"

    with pytest.raises(ManifestError, match="unknown=\\['comment'\\]"):
        manifest_from_dict(payload)


def test_manifest_rejects_a_wrong_schema_version(tmp_path: Path) -> None:
    """Only ``1.0`` is readable."""
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    payload = manifest_to_dict(make_manifest((entry,), total_points=8))
    payload["schema_version"] = "1.1"

    with pytest.raises(ManifestError, match="schema_version"):
        manifest_from_dict(payload)


def test_manifest_rejects_an_unknown_status(tmp_path: Path) -> None:
    """``status`` is a closed set; a typo must not become "not complete"."""
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    payload = manifest_to_dict(make_manifest((entry,), total_points=8))
    payload["status"] = "running"

    with pytest.raises(ManifestError, match="manifest.status"):
        manifest_from_dict(payload)


def test_complete_manifest_must_cover_total_points(tmp_path: Path) -> None:
    """A complete run whose parts do not add up is a lie about the data."""
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )

    with pytest.raises(ManifestError, match="cover total_points"):
        make_manifest((entry,), total_points=1_000_000)


def test_failed_manifest_may_be_partial(tmp_path: Path) -> None:
    """A ``failed`` manifest is allowed to describe an incomplete run."""
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    manifest = make_manifest(
        (entry,), total_points=1_000_000, status=ManifestStatus.FAILED
    )

    assert manifest.status is ManifestStatus.FAILED


def test_manifest_rejects_non_contiguous_parts(tmp_path: Path) -> None:
    """``part_id`` must run 0, 1, 2… with no gap and no duplicate."""
    first = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path, 0), run_dir=tmp_path
    )
    third = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path, 2), run_dir=tmp_path
    )

    with pytest.raises(ManifestError, match="contiguous from 0"):
        make_manifest((first, third), total_points=16)


def test_entry_rejects_a_malformed_digest() -> None:
    """The digest is a fixed-width contract, not a free-form string."""
    for bad in ("abc", "A" * 64, "g" * 64):
        with pytest.raises(ManifestError, match="sha256"):
            ChunkManifestEntry(
                part_id=0, path="raw/part-000000.npz", rows=1, sha256=bad
            )


def test_entry_rejects_non_positive_rows() -> None:
    """An empty part cannot be described by the manifest."""
    with pytest.raises(ManifestError, match="rows must be positive"):
        ChunkManifestEntry(
            part_id=0, path="raw/part-000000.npz", rows=0, sha256="0" * 64
        )
