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
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

import numpy as np
import pytest

from g15.pipeline.schema import (
    NPZ_FIELDS,
    SCHEMA_VERSION,
    ChunkManifestEntry,
    ChunkValidationError,
    DataContractError,
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
from g15.common.types import SimulationChunk

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


def test_digest_valid_but_not_a_zip_is_refused(tmp_path: Path) -> None:
    """A damaged file must fail inside the contract family, never as a raw error.

    ``zipfile.BadZipFile`` inherits ``Exception`` rather than ``OSError``, so it
    used to escape ``read_chunk`` untouched and reach the CLI as a traceback
    instead of exit code 3 (``docs/interface.md`` section 12, corrupted-file
    isolation).  The digest here is correct on purpose: the failure has to come
    from parsing, not from the checksum check.
    """
    path = shard_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(
        b"PK\x03\x04 truncated: the digest matches, the bytes are not a zip"
    )
    entry = ChunkManifestEntry(
        part_id=0, path="raw/part-000000.npz", rows=8, sha256=sha256_of_file(path)
    )

    with pytest.raises(ChunkIOError, match="cannot read shard") as excinfo:
        read_chunk(path, entry)

    assert isinstance(excinfo.value, DataContractError)
    assert not isinstance(excinfo.value, zipfile.BadZipFile)
    assert sha256_of_file(path) == entry.sha256


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


def test_part_id_contradicting_the_name_keeps_the_old_shard(tmp_path: Path) -> None:
    """A rejected ``part_id`` must not overwrite the shard.

    Regression for the PR #29 review: the destination used to be replaced before
    the entry was built, so passing ``part_id=1`` for ``part-000000.npz``
    published different bytes and only then returned an entry whose index and
    file name disagreed.  The payload is shifted by +5 g on purpose, so any leak
    of the rejected chunk into the old file changes its bytes and its digest.
    """
    path = shard_path(tmp_path, part_id=0)
    accepted = write_chunk_atomic(make_chunk(count=8), path, run_dir=tmp_path)
    before = path.read_bytes()

    base = make_chunk(count=8)
    shifted = SimulationChunk(
        seq=base.seq,
        timestamp_ns=base.timestamp_ns,
        vibration=base.vibration + 5.0,
        temperature=base.temperature,
    )

    with pytest.raises(ChunkIOError, match="encodes part_id 0"):
        write_chunk_atomic(shifted, path, run_dir=tmp_path, part_id=1)

    assert path.read_bytes() == before
    assert sha256_of_file(path) == accepted.sha256
    assert read_chunk(path, accepted).seq.shape[0] == 8
    assert [item.name for item in path.parent.iterdir()] == ["part-000000.npz"]


def test_shard_outside_run_dir_creates_nothing(tmp_path: Path) -> None:
    """A rejected destination must not exist afterwards.

    Regression for the PR #29 review: ``os.replace`` ran before the containment
    check, so the failing call still left ``outside/part-000001.npz`` on disk
    while the manifest knew nothing about it.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside" / "part-000001.npz"

    with pytest.raises(ChunkIOError, match="not inside run directory"):
        write_chunk_atomic(make_chunk(), outside, run_dir=run_dir)

    assert not outside.exists()
    assert not outside.parent.exists()
    assert list(run_dir.iterdir()) == []


def test_negative_part_id_creates_nothing(tmp_path: Path) -> None:
    """An impossible index is refused before the directory is even created."""
    path = tmp_path / "raw" / "chunk.npz"

    with pytest.raises(ChunkIOError, match="part_id must be non-negative"):
        write_chunk_atomic(make_chunk(), path, run_dir=tmp_path, part_id=-1)

    assert not path.parent.exists()


# --------------------------------------------------------------------------- #
# validate_chunk and path rules
# --------------------------------------------------------------------------- #


def test_validate_chunk_accepts_a_contract_compliant_shard() -> None:
    """The happy path must not raise; it returns ``None`` on success."""
    assert validate_chunk(make_chunk(count=32, start_seq=512)) is None


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


@pytest.mark.parametrize(
    "units",
    [
        {"vibration": "m/s2", "temperature": "degC"},
        {"vibration": "g", "temperature": "K"},
        {"vibration": "", "temperature": ""},
        {"vibration": "g ", "temperature": "degC"},
    ],
)
def test_units_must_match_the_frozen_values(
    tmp_path: Path, units: dict[str, str]
) -> None:
    """Section 5.2 freezes the unit *values*, not just the key set.

    ``m/s2`` accepted here would be published as if it were ``g`` and every
    downstream consumer would silently interpret the amplitude with the wrong
    magnitude -- a data-correctness bug that no later check can detect.
    """
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    manifest = make_manifest((entry,), total_points=8)

    with pytest.raises(ManifestError, match="frozen by section 5.2"):
        replace(manifest, units=units)


def test_manifest_from_dict_does_not_coerce_unit_types(tmp_path: Path) -> None:
    """Non-string units are refused, not silently turned into ``"123"``."""
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    payload = manifest_to_dict(make_manifest((entry,), total_points=8))
    payload["units"] = {"vibration": 123, "temperature": 456}

    with pytest.raises(ManifestError, match="frozen by section 5.2"):
        manifest_from_dict(payload)


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


# --------------------------------------------------------------------------- #
# Issue #34, item 1: part_id and the file name must agree
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("part_id", "path"),
    [
        (0, "raw/part-000005.npz"),
        (5, "raw/part-000000.npz"),
        (3, "raw/nested/part-000004.npz"),
        (999_999, "raw/part-1000000.npz"),
    ],
)
def test_entry_rejects_a_part_id_that_contradicts_the_file_name(
    part_id: int, path: str
) -> None:
    """A canonical name encodes the index, so the two must not disagree.

    ``part_id=0`` + ``raw/part-000005.npz`` used to be accepted: the manifest
    claimed one shard while the path pointed at another, and every consumer that
    trusted the entry on the number and the file system on the path read the
    wrong 102 400 samples.
    """
    with pytest.raises(ManifestError, match="encodes part_id"):
        ChunkManifestEntry(part_id=part_id, path=path, rows=8, sha256="0" * 64)


@pytest.mark.parametrize(
    ("part_id", "path"),
    [
        (0, "raw/part-000000.npz"),
        (5, "raw/part-000005.npz"),
        # Parent directories are not frozen by section 5.2, so nesting is fine.
        (3, "raw/deep/nested/part-000003.npz"),
        # Six digits is the minimum, not the maximum.
        (1_000_000, "raw/part-1000000.npz"),
        # A bare name carries the same information as one with a parent.
        (0, "part-000000.npz"),
        # A non-canonical name encodes no number, so nothing can contradict it.
        (4, "raw/shard-4.npz"),
    ],
)
def test_entry_accepts_a_path_consistent_with_its_part_id(
    part_id: int, path: str
) -> None:
    """Only contradiction is refused; the check never narrows valid paths."""
    entry = ChunkManifestEntry(part_id=part_id, path=path, rows=8, sha256="0" * 64)

    assert entry.part_id == part_id
    assert entry.path == path


def test_manifest_from_dict_rejects_a_part_id_that_contradicts_the_path(
    tmp_path: Path,
) -> None:
    """The read path must apply the same rule, not only direct construction."""
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    payload = manifest_to_dict(make_manifest((entry,), total_points=8))
    payload["parts"][0]["path"] = "raw/part-000007.npz"

    with pytest.raises(ManifestError, match="encodes part_id 7"):
        manifest_from_dict(payload)


# --------------------------------------------------------------------------- #
# Issue #34, item 2: start_time_utc is a complete UTC-anchored instant
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        "2026-10-19",  # a bare date: fromisoformat silently means midnight
        "2026-10-19T00:00:00",  # no timezone at all
        "2026-10-19T00:00:00+08:00",  # a real offset, but not UTC
        "2026-10-19T00:00:00-05:00",
        "20261019T000000Z",  # basic format, not the documented shape
        "2026-13-45T99:99:99Z",  # matches the shape, is not a real instant
        "2026-02-30T00:00:00Z",
        "2026-10-19T00:00:00ZZ",
        "not-a-timestamp",
        "",
        "   ",
    ],
)
def test_manifest_rejects_an_incomplete_or_non_utc_start_time(
    tmp_path: Path, bad: str
) -> None:
    """A timestamp nobody can place on a timeline must not be published.

    Only a suffix check would accept ``2026-13-45T99:99:99Z``; only
    ``fromisoformat`` would accept the bare date and the naive form.
    """
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    manifest = make_manifest((entry,), total_points=8)

    with pytest.raises(ManifestError, match="start_time_utc"):
        replace(manifest, start_time_utc=bad)


def test_manifest_rejects_a_non_string_start_time(tmp_path: Path) -> None:
    """``None`` is not a timestamp, and must not reach a consumer either."""
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    manifest = make_manifest((entry,), total_points=8)

    with pytest.raises(ManifestError, match="start_time_utc"):
        replace(manifest, start_time_utc=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "good",
    [
        "2026-10-14T00:00:00Z",
        # The producer copies the configuration text verbatim, and
        # g15.config accepts a zero offset, so refusing it here would make a
        # run unreadable by its own reader.
        "2026-10-14T00:00:00+00:00",
        "2026-10-14T00:00:00.123456Z",
        "2024-02-29T23:59:59Z",  # a real leap day
    ],
)
def test_manifest_accepts_a_complete_utc_start_time(tmp_path: Path, good: str) -> None:
    """Both UTC spellings in use across the project stay readable."""
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    manifest = make_manifest((entry,), total_points=8)

    assert replace(manifest, start_time_utc=good).start_time_utc == good


def test_manifest_from_dict_applies_the_start_time_rule(tmp_path: Path) -> None:
    """The parser must not be the lenient door into the same dataclass."""
    entry = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path), run_dir=tmp_path
    )
    payload = manifest_to_dict(make_manifest((entry,), total_points=8))
    payload["start_time_utc"] = "2026-10-19T00:00:00"

    with pytest.raises(ManifestError, match="start_time_utc"):
        manifest_from_dict(payload)


# --------------------------------------------------------------------------- #
# Issue #34, item 3: a shard must live where the entry says it lives
# --------------------------------------------------------------------------- #


def test_read_chunk_refuses_a_byte_identical_copy_outside_the_run(
    tmp_path: Path,
) -> None:
    """A matching name and digest is not proof of ownership.

    Copying ``raw/part-000000.npz`` to another directory keeps both checks
    green, so the reader trusted data the run never published.  Passing
    ``run_dir`` turns the match into a real containment check.
    """
    run_dir = tmp_path / "run"
    path = shard_path(run_dir)
    entry = write_chunk_atomic(make_chunk(), path, run_dir=run_dir)

    outside = tmp_path / "elsewhere" / "raw" / "part-000000.npz"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(path.read_bytes())
    assert outside.name == PurePosixPath(entry.path).name
    assert sha256_of_file(outside) == entry.sha256

    with pytest.raises(ChunkIOError, match="outside run directory"):
        read_chunk(outside, entry, run_dir=run_dir)


def test_read_chunk_accepts_the_published_location(tmp_path: Path) -> None:
    """The containment check must not reject the legitimate file."""
    entry = write_chunk_atomic(make_chunk(), shard_path(tmp_path), run_dir=tmp_path)

    assert read_chunk(shard_path(tmp_path), entry, run_dir=tmp_path).seq.shape[0] == 8


def test_read_chunk_refuses_a_shard_at_another_relative_path(tmp_path: Path) -> None:
    """Inside the run is not enough: the relative path must be the recorded one."""
    path = shard_path(tmp_path)
    entry = write_chunk_atomic(make_chunk(), path, run_dir=tmp_path)
    moved = ChunkManifestEntry(
        part_id=0, path="other/part-000000.npz", rows=8, sha256=entry.sha256
    )

    with pytest.raises(ChunkIOError, match="but the manifest records"):
        read_chunk(path, moved, run_dir=tmp_path)


def test_read_chunk_refuses_a_symlink_that_escapes_the_run(tmp_path: Path) -> None:
    """Resolving the link before the containment test blocks the escape."""
    run_dir = tmp_path / "run"
    entry = write_chunk_atomic(make_chunk(), shard_path(run_dir), run_dir=run_dir)

    decoy = tmp_path / "decoy.npz"
    decoy.write_bytes((run_dir / "raw" / "part-000000.npz").read_bytes())
    link = run_dir / "raw" / "part-000000.npz"
    link.unlink()
    try:
        link.symlink_to(decoy)
    except (OSError, NotImplementedError):  # pragma: no cover - needs the privilege
        pytest.skip("this platform cannot create symlinks without elevation")
    if not link.is_symlink():  # pragma: no cover - some Windows filesystems no-op
        pytest.skip("the filesystem reported success but created no link")

    with pytest.raises(ChunkIOError, match="outside run directory"):
        read_chunk(link, entry, run_dir=run_dir)


def test_read_chunk_without_run_dir_is_documented_as_weak(tmp_path: Path) -> None:
    """Opting out of ``run_dir`` keeps only the name-and-digest checks.

    This is the documented contract, not an oversight: a caller that does not
    know the run directory cannot prove ownership, so the *consumer* entry point
    must pass ``run_dir`` (Issue #34, item 3).  Pinning the weak behaviour here
    makes the trade-off visible instead of implicit.
    """
    run_dir = tmp_path / "run"
    entry = write_chunk_atomic(make_chunk(), shard_path(run_dir), run_dir=run_dir)

    outside = tmp_path / "elsewhere" / "part-000000.npz"
    outside.parent.mkdir(parents=True)
    outside.write_bytes((run_dir / "raw" / "part-000000.npz").read_bytes())

    assert read_chunk(outside, entry).seq.shape[0] == 8
    with pytest.raises(ChunkIOError, match="outside run directory"):
        read_chunk(outside, entry, run_dir=run_dir)


# --------------------------------------------------------------------------- #
# Issue #34, item 4: a failed run may publish zero shards
# --------------------------------------------------------------------------- #


def test_failed_manifest_may_publish_no_parts() -> None:
    """A run that died before the first shard was durable must be expressible.

    The old rule refused ``parts=()`` for every status, so a producer that died
    during setup had no honest manifest to write: it either lied with
    ``complete`` or wrote nothing at all.
    """
    manifest = make_manifest((), total_points=1_000_000, status=ManifestStatus.FAILED)

    assert manifest.parts == ()
    assert manifest.status is ManifestStatus.FAILED


def test_complete_manifest_must_publish_at_least_one_part() -> None:
    """Relaxing ``failed`` must not open the door for ``complete``."""
    with pytest.raises(ManifestError, match="must list at least one part"):
        make_manifest((), total_points=8)


def test_failed_manifest_without_parts_round_trips_through_json() -> None:
    """The empty list survives serialisation, so a consumer can see the failure."""
    manifest = make_manifest((), total_points=1_000_000, status=ManifestStatus.FAILED)

    payload = json.loads(json.dumps(manifest_to_dict(manifest)))

    assert payload["status"] == "failed"
    assert payload["parts"] == []
    assert manifest_from_dict(payload) == manifest


def test_failed_manifest_parts_are_still_contiguous(tmp_path: Path) -> None:
    """A partial run may stop early, but it may not skip a shard."""
    first = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path, 0), run_dir=tmp_path
    )
    third = write_chunk_atomic(
        make_chunk(count=8), shard_path(tmp_path, 2), run_dir=tmp_path
    )

    with pytest.raises(ManifestError, match="contiguous from 0"):
        make_manifest((first, third), total_points=16, status=ManifestStatus.FAILED)
