"""Integration tests for the independent post-production verifier.

Every case drives :func:`g15.reporting.verifier.verify_run` against a run that
``produce_run_detailed`` really published, then damages exactly one thing so the
verdict can only come from the verifier.

The isolation cases matter as much as the damage cases: a verifier that quietly
read ``private_truth/`` or the consumer database would pass every damage test
while proving nothing, so those two are asserted by *removing* the input and
requiring the same verdict.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from g15.pipeline.producer import produce_run_detailed
from g15.pipeline.schema import manifest_to_dict
from g15.reporting.verifier import (
    VERIFICATION_CHECKS,
    VerificationError,
    verify_run,
)

CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def scenario_config(
    total_points: int = 1024,
    chunk_points: int = 256,
) -> dict[str, object]:
    """Return a configuration shaped like ``docs/interface.md`` section 4."""
    return {
        "schema_version": "1.0",
        "scenario": {
            "id": "verify_001",
            "seed": 42,
            "start_time_utc": "2026-10-14T00:00:00Z",
            "device_id": "DEV-001",
            "sample_rate_hz": 100,
            "total_points": total_points,
            "chunk_points": chunk_points,
        },
        "signals": {
            "vibration": {
                "baseline": 0.5,
                "degradation_rate_per_sample": 1e-06,
                "noise_std": 0.05,
                "unit": "g",
            },
            "temperature": {
                "baseline": 35.0,
                "degradation_rate_per_sample": 3e-06,
                "noise_std": 0.15,
                "unit": "degC",
            },
        },
        "fault_generation": {
            "mode": "manual",
            "stochastic_probability_per_window": 0.0,
            "events": [],
        },
    }


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    """Publish one four-shard run and return its directory."""
    summary = produce_run_detailed(scenario_config(), tmp_path / "runs")
    return summary.run_dir


def _read_manifest_payload(run_dir: Path) -> dict[str, Any]:
    """Return the manifest of ``run_dir`` as a plain dictionary."""
    payload: dict[str, Any] = json.loads(
        (run_dir / "manifest.json").read_text(encoding="utf-8")
    )
    return payload


def _rewrite_manifest(run_dir: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` back as the manifest of ``run_dir``."""
    (run_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _rewrite_shard(run_dir: Path, relative: str, **overrides: Any) -> None:
    """Rewrite one shard's arrays in place, keeping the other fields."""
    path = run_dir / relative
    with np.load(path, allow_pickle=False) as archive:
        arrays = {name: np.asarray(archive[name]) for name in archive.files}
    arrays.update(overrides)
    np.savez(path, **arrays)


# --------------------------------------------------------------------------- #
# A clean run
# --------------------------------------------------------------------------- #


def test_a_published_run_verifies(run_dir: Path) -> None:
    """A run the producer just finished must pass every check."""
    result = verify_run(run_dir)

    assert result.passed is True
    assert result.failures == ()
    assert result.status == "complete"
    assert result.total_points == 1024
    assert result.rows_expected_sum == 1024
    assert result.rows_sum == 1024
    assert result.part_count == 4
    assert result.verified_part_count == 4
    assert result.scalar_value_count == 2048
    assert result.first_seq == 0
    assert result.last_seq == 1023
    assert result.shard_shape == "4x256"
    assert result.elapsed_seconds > 0.0
    assert result.run_id == run_dir.name


def test_result_payload_quotes_the_documented_check_list(run_dir: Path) -> None:
    """``to_dict`` must name the same checks the module advertises."""
    payload = verify_run(run_dir).to_dict()

    assert payload["checks"] == list(VERIFICATION_CHECKS)
    assert payload["passed"] is True
    assert payload["failures"] == []
    assert payload["shards"]
    first = payload["shards"][0]
    assert first["part_id"] == 0
    assert first["path"] == "raw/part-000000.npz"
    assert first["expected_rows"] == 256
    assert first["observed_rows"] == 256
    assert first["expected_sha256"] == first["observed_sha256"]
    assert first["dtype_ok"] is True
    assert first["seq_ok"] is True
    assert first["hash_ok"] is True
    assert first["failures"] == []


def test_verification_does_not_depend_on_the_answer_key(run_dir: Path) -> None:
    """Deleting ``private_truth/`` must not change the verdict."""
    before = verify_run(run_dir)
    shutil.rmtree(run_dir / "private_truth", ignore_errors=True)

    after = verify_run(run_dir)

    assert after.passed is True
    assert after.failures == ()
    assert after.rows_sum == before.rows_sum


def test_verification_does_not_open_the_database(run_dir: Path) -> None:
    """A run with no SQLite database at all must still verify."""
    assert not (run_dir / "artifacts" / "results.sqlite").exists()

    result = verify_run(run_dir)

    assert result.passed is True
    assert not (run_dir / "artifacts" / "results.sqlite").exists()


def test_verification_never_opens_the_answer_key_or_the_database(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both files are present but must never be opened.

    Deleting them and requiring success is not proof: a verifier that reads them
    *optionally* would still pass that test.  Here they exist and every attempt
    to open either path fails the test, so an opportunistic read cannot hide.
    """
    truth = run_dir / "private_truth" / "faults.json"
    database = run_dir / "artifacts" / "results.sqlite"
    truth.parent.mkdir(parents=True, exist_ok=True)
    database.parent.mkdir(parents=True, exist_ok=True)
    truth.write_text('{"run_id": "decoy", "events": []}', encoding="utf-8")
    database.write_bytes(b"SQLite format 3\x00decoy")

    opened: list[str] = []
    real_open = open
    real_path_open = Path.open

    def _guard(name: str) -> None:
        if "private_truth" in name or "results.sqlite" in name:
            opened.append(name)
            raise AssertionError(f"the verifier opened {name}")

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        _guard(str(file))
        return real_open(file, *args, **kwargs)

    def guarded_path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        _guard(str(self))
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)

    result = verify_run(run_dir)

    assert opened == []
    assert result.passed is True
    assert result.failures == ()


def test_verification_writes_nothing(run_dir: Path) -> None:
    """Verifying must leave the run directory byte-for-byte identical."""
    before = sorted(
        (path.relative_to(run_dir).as_posix(), path.stat().st_size)
        for path in run_dir.rglob("*")
        if path.is_file()
    )

    verify_run(run_dir)

    after = sorted(
        (path.relative_to(run_dir).as_posix(), path.stat().st_size)
        for path in run_dir.rglob("*")
        if path.is_file()
    )
    assert after == before


# --------------------------------------------------------------------------- #
# Damaged shards
# --------------------------------------------------------------------------- #


def test_a_missing_shard_is_reported_not_raised(run_dir: Path) -> None:
    """A deleted shard is a failure entry, so one pass can list them all."""
    (run_dir / "raw" / "part-000001.npz").unlink()

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.missing" in result.failures
    assert result.verified_part_count == 3
    assert result.rows_sum == 768
    assert result.rows_expected_sum == 1024
    assert result.shards[1].observed_rows is None
    assert result.shards[1].hash_ok is None


def test_a_flipped_byte_is_a_digest_failure(run_dir: Path) -> None:
    """Content that does not match the published bytes is never parsed."""
    path = run_dir / "raw" / "part-000002.npz"
    payload = bytearray(path.read_bytes())
    payload[100] ^= 0xFF
    path.write_bytes(bytes(payload))

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.hash_mismatch" in result.failures
    shard = result.shards[2]
    assert shard.hash_ok is False
    assert shard.observed_sha256 is not None
    assert shard.observed_sha256 != shard.expected_sha256
    # Unverified content must not be described as a dtype or seq failure.
    assert shard.dtype_ok is None
    assert shard.seq_ok is None
    assert shard.observed_rows is None


def test_an_unreadable_archive_is_reported(run_dir: Path) -> None:
    """A file with the right digest but no zip inside is a failure."""
    path = run_dir / "raw" / "part-000000.npz"
    path.write_bytes(b"not a zip archive at all")
    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(path)
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.unreadable" in result.failures
    assert result.shards[0].hash_ok is True
    assert result.shards[0].observed_rows is None


def test_a_wrong_dtype_is_reported(run_dir: Path) -> None:
    """``seq`` stored as float64 violates the frozen dtype."""
    _rewrite_shard(
        run_dir,
        "raw/part-000000.npz",
        seq=np.arange(0, 256, dtype=np.float64),
    )
    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(run_dir / "raw" / "part-000000.npz")
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.dtype_mismatch" in result.failures
    assert result.shards[0].dtype_ok is False


def test_a_gap_inside_a_shard_is_reported(run_dir: Path) -> None:
    """``seq`` must advance by exactly one inside one shard."""
    seq = np.arange(0, 256, dtype=np.int64)
    seq[100] = 999
    _rewrite_shard(run_dir, "raw/part-000000.npz", seq=seq)
    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(run_dir / "raw" / "part-000000.npz")
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.seq_gap" in result.failures
    assert result.shards[0].seq_ok is False


def test_a_short_shard_is_reported(run_dir: Path) -> None:
    """The observed row count is compared with the declared one."""
    seq = np.arange(0, 100, dtype=np.int64)
    _rewrite_shard(
        run_dir,
        "raw/part-000000.npz",
        seq=seq,
        timestamp_ns=np.arange(0, 100, dtype=np.int64),
        vibration=np.zeros(100, dtype=np.float64),
        temperature=np.zeros(100, dtype=np.float64),
    )
    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(run_dir / "raw" / "part-000000.npz")
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.rows_mismatch" in result.failures
    shard = result.shards[0]
    assert shard.expected_rows == 256
    assert shard.observed_rows == 100


def test_a_gap_between_shards_is_reported(run_dir: Path) -> None:
    """``seq`` must continue across the shard boundary."""
    seq = np.arange(256, 512, dtype=np.int64) + 5
    _rewrite_shard(run_dir, "raw/part-000001.npz", seq=seq)
    payload = _read_manifest_payload(run_dir)
    payload["parts"][1]["sha256"] = _sha256(run_dir / "raw" / "part-000001.npz")
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "seq.cross_shard_gap" in result.failures


def test_a_run_not_starting_at_zero_is_reported(run_dir: Path) -> None:
    """The first shard must begin at ``seq`` zero."""
    seq = np.arange(0, 256, dtype=np.int64) + 1
    _rewrite_shard(run_dir, "raw/part-000000.npz", seq=seq)
    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(run_dir / "raw" / "part-000000.npz")
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "seq.not_from_zero" in result.failures


# --------------------------------------------------------------------------- #
# Structurally invalid manifests
# --------------------------------------------------------------------------- #


def test_a_missing_manifest_raises(run_dir: Path) -> None:
    """No manifest means the run cannot be verified at all."""
    (run_dir / "manifest.json").unlink()

    with pytest.raises(VerificationError):
        verify_run(run_dir)


def test_a_malformed_manifest_raises(run_dir: Path) -> None:
    """Unparseable JSON is an error, not a negative verdict."""
    (run_dir / "manifest.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(VerificationError):
        verify_run(run_dir)


def test_a_manifest_that_does_not_cover_the_run_raises(run_dir: Path) -> None:
    """Section 5.2 already refuses a short ``complete`` manifest."""
    payload = _read_manifest_payload(run_dir)
    payload["total_points"] = 4096
    _rewrite_manifest(run_dir, payload)

    with pytest.raises(VerificationError):
        verify_run(run_dir)


def test_a_non_contiguous_manifest_raises(run_dir: Path) -> None:
    """A missing middle entry is refused by the manifest itself."""
    payload = _read_manifest_payload(run_dir)
    del payload["parts"][1]
    _rewrite_manifest(run_dir, payload)

    with pytest.raises(VerificationError):
        verify_run(run_dir)


def test_an_unsafe_entry_path_raises(run_dir: Path) -> None:
    """An entry that escapes the run directory is a safety error."""
    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["path"] = "../outside.npz"
    _rewrite_manifest(run_dir, payload)

    with pytest.raises(VerificationError):
        verify_run(run_dir)


def test_a_symlink_escape_raises(run_dir: Path, tmp_path: Path) -> None:
    """A symlink pointing out of the run is refused, not followed."""
    outside = tmp_path / "outside.npz"
    shutil.copyfile(run_dir / "raw" / "part-000000.npz", outside)
    link = run_dir / "raw" / "part-000000.npz"
    link.unlink()
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - platform
        pytest.skip("this platform cannot create symlinks")
    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(outside)
    _rewrite_manifest(run_dir, payload)

    with pytest.raises(VerificationError):
        verify_run(run_dir)


# --------------------------------------------------------------------------- #
# Caller-supplied expectations
# --------------------------------------------------------------------------- #


def test_the_expected_contour_is_checked(run_dir: Path) -> None:
    """Verifying against the wrong contour must fail, not pass quietly."""
    result = verify_run(run_dir, expected_total_points=1_000_000)

    assert result.passed is False
    assert "rows.expected_mismatch" in result.failures


def test_the_right_contour_passes(run_dir: Path) -> None:
    """A matching expectation adds no failure."""
    result = verify_run(run_dir, expected_total_points=1024)

    assert result.passed is True
    assert result.failures == ()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1024"])
def test_an_invalid_expectation_is_a_caller_error(run_dir: Path, value: object) -> None:
    """A bad argument is a programming error, not a verdict about the run."""
    with pytest.raises(ValueError):
        verify_run(run_dir, expected_total_points=value)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# A real acceptance-shaped run
# --------------------------------------------------------------------------- #


def test_the_acceptance_run_verifies_end_to_end(tmp_path: Path) -> None:
    """The documented acceptance scenario must verify against its own contour."""
    from g15.config import load_config

    summary = produce_run_detailed(
        load_config(CONFIGS / "acceptance_spike_seed42.yaml"), tmp_path / "runs"
    )

    result = verify_run(summary.run_dir, expected_total_points=summary.total_points)

    assert result.passed is True
    assert result.rows_sum == summary.total_points
    assert result.part_count == summary.part_count
    assert result.last_seq == summary.total_points - 1
    assert result.elapsed_seconds > 0.0
    payload = result.to_dict()
    assert payload["checks"] == list(VERIFICATION_CHECKS)


def test_the_manifest_round_trips_through_the_public_helper(run_dir: Path) -> None:
    """Rewriting a manifest with ``manifest_to_dict`` keeps the run verifiable."""
    from g15.pipeline.schema import manifest_from_dict

    payload = _read_manifest_payload(run_dir)
    manifest = manifest_from_dict(payload)
    _rewrite_manifest(run_dir, manifest_to_dict(manifest))

    assert verify_run(run_dir).passed is True


def _sha256(path: Path) -> str:
    """Return the SHA-256 of ``path`` as 64 lower-case hex characters."""
    import hashlib

    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Section 5.1: four parallel columns of one table
#
# The digest gate alone cannot catch these: the manifest is rewritten with the
# new digest, so the bytes are exactly the bytes the manifest declares.  What is
# wrong is the *shape* of the table, and a verifier that only looks at
# ``seq.shape[0]`` would call all three of them clean.
# --------------------------------------------------------------------------- #


def _shorten(run_dir: Path, field: str, rows: int) -> None:
    """Replace one column of part 0 with its first ``rows`` entries."""
    path = run_dir / "raw" / "part-000000.npz"
    with np.load(path, allow_pickle=False) as archive:
        arrays = {name: np.asarray(archive[name]) for name in archive.files}
    arrays[field] = arrays[field][:rows]
    np.savez(path, **arrays)
    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(path)
    _rewrite_manifest(run_dir, payload)


@pytest.mark.parametrize("field", ["vibration", "temperature", "timestamp_ns"])
def test_a_column_that_is_not_as_long_as_seq_is_reported(
    run_dir: Path, field: str
) -> None:
    """Section 5.1: the four arrays are columns of one table.

    ``seq`` is left at 256 rows on purpose -- the only broken thing is that one
    column no longer has 256 entries.  Reporting ``passed`` here would mean
    claiming ``2 x rows`` signal scalars were verified when one column is short.
    """
    _shorten(run_dir, field, 255)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.length_mismatch" in result.failures
    shard = result.shards[0]
    assert shard.dtype_ok is True  # the remaining values are still legal
    assert shard.observed_rows == 256  # seq is intact
    # The short column is not a verified table, so it must not be counted as
    # 2 x rows verified scalars.
    assert result.verified_part_count == 3
    assert result.rows_sum == 768


def test_an_empty_signal_column_is_reported(run_dir: Path) -> None:
    """Zero rows in ``vibration`` against 256 in ``seq`` is the same defect."""
    _shorten(run_dir, "vibration", 0)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.length_mismatch" in result.failures


def test_a_length_mismatch_does_not_hide_the_remaining_shards(
    run_dir: Path,
) -> None:
    """One bad column must not stop the other three shards from being checked."""
    _shorten(run_dir, "temperature", 100)

    result = verify_run(run_dir)

    assert result.passed is False
    assert result.part_count == 4
    assert result.verified_part_count == 3
    assert all(shard.hash_ok is True for shard in result.shards)


# --------------------------------------------------------------------------- #
# Damaged ``seq`` must be reported, never raised
#
# ``seq`` is read twice: once to check the column, once to record the boundary
# values.  The second read must not run when the first already failed, or a NaN
# column would raise ``ValueError`` out of a content check and abandon the
# shards that follow.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_value", [np.nan, np.inf, -np.inf])
def test_a_non_integer_seq_column_is_reported_not_raised(
    run_dir: Path, bad_value: float
) -> None:
    """A float ``seq`` full of NaN/Inf is content damage, not a caller error."""
    path = run_dir / "raw" / "part-000000.npz"
    with np.load(path, allow_pickle=False) as archive:
        arrays = {name: np.asarray(archive[name]) for name in archive.files}
    arrays["seq"] = np.full(arrays["seq"].shape, bad_value, dtype=np.float64)
    np.savez(path, **arrays)
    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(path)
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)  # must not raise

    assert result.passed is False
    assert "shard.dtype_mismatch" in result.failures
    shard = result.shards[0]
    assert shard.dtype_ok is False
    assert shard.seq_ok is False
    # Unreadable boundary stays unknown instead of being fabricated.
    assert shard.first_seq is None
    assert shard.last_seq is None
    # The other three shards are still inspected.
    assert result.verified_part_count == 3


def test_a_broken_seq_does_not_stop_later_shards(run_dir: Path) -> None:
    """A NaN ``seq`` in part 0 plus a real gap in part 2 must both be reported."""
    path = run_dir / "raw" / "part-000000.npz"
    with np.load(path, allow_pickle=False) as archive:
        arrays = {name: np.asarray(archive[name]) for name in archive.files}
    arrays["seq"] = np.full(arrays["seq"].shape, np.nan, dtype=np.float64)
    np.savez(path, **arrays)

    gap_path = run_dir / "raw" / "part-000002.npz"
    with np.load(gap_path, allow_pickle=False) as archive:
        gap = {name: np.asarray(archive[name]) for name in archive.files}
    seq = gap["seq"].copy()
    seq[10] = 999
    gap["seq"] = seq
    np.savez(gap_path, **gap)

    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(path)
    payload["parts"][2]["sha256"] = _sha256(gap_path)
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.dtype_mismatch" in result.failures
    assert "shard.seq_gap" in result.failures


# --------------------------------------------------------------------------- #
# The container itself can be wrong
# --------------------------------------------------------------------------- #


def test_an_npy_payload_in_place_of_an_npz_is_reported(run_dir: Path) -> None:
    """A legal ``.npy`` file saved under the shard name is not an NPZ archive.

    ``np.load`` returns a bare ``ndarray`` for those bytes, so this case used to
    escape as ``TypeError`` (no context-manager protocol) instead of a shard
    verdict.
    """
    path = run_dir / "raw" / "part-000000.npz"
    with np.load(path, allow_pickle=False) as archive:
        payload_array = np.asarray(archive["vibration"])
    npy = run_dir / "raw" / "payload.npy"
    np.save(npy, payload_array)
    shutil.copy(npy, path)
    npy.unlink()

    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(path)
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)  # must not raise TypeError

    assert result.passed is False
    assert "shard.not_archive" in result.failures
    assert result.shards[0].hash_ok is True
    assert result.shards[0].observed_rows is None
    assert result.verified_part_count == 3


def test_an_npy_payload_does_not_stop_the_other_shards(run_dir: Path) -> None:
    """The remaining shards are still hashed and checked after a bad container."""
    path = run_dir / "raw" / "part-000001.npz"
    with np.load(path, allow_pickle=False) as archive:
        payload_array = np.asarray(archive["temperature"])
    npy = run_dir / "raw" / "payload.npy"
    np.save(npy, payload_array)
    shutil.copy(npy, path)
    npy.unlink()

    payload = _read_manifest_payload(run_dir)
    payload["parts"][1]["sha256"] = _sha256(path)
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.not_archive" in result.failures
    assert result.verified_part_count == 3
    assert result.shards[2].hash_ok is True
    assert result.shards[3].hash_ok is True


# --------------------------------------------------------------------------- #
# The origin check belongs to part 0 only
# --------------------------------------------------------------------------- #


def test_an_unreadable_first_shard_does_not_blame_the_second(
    run_dir: Path,
) -> None:
    """``seq.not_from_zero`` is a statement about part 0, not about whoever is
    readable first.

    If part 0 is unreadable and part 1 starts at 256, the chain baseline is
    unknown; reporting ``seq.not_from_zero`` would blame a healthy shard.
    """
    path = run_dir / "raw" / "part-000000.npz"
    path.unlink()

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.missing" in result.failures
    assert "seq.not_from_zero" not in result.failures


# --------------------------------------------------------------------------- #
# A column with no row axis at all
#
# The length check reads shape[0], which does not exist on a 0-d array.  A
# shard damaged that way must still produce a per-shard verdict: raising
# IndexError here would abandon every shard after it, which is the same class
# of bug as the NaN ``seq`` case one layer up.
# --------------------------------------------------------------------------- #


def _replace_column(run_dir: Path, field: str, value: Any) -> None:
    """Replace one column of part 0 with ``value`` and refresh the digest."""
    path = run_dir / "raw" / "part-000000.npz"
    with np.load(path, allow_pickle=False) as archive:
        arrays = {name: np.asarray(archive[name]) for name in archive.files}
    arrays[field] = np.asarray(value)
    np.savez(path, **arrays)
    payload = _read_manifest_payload(run_dir)
    payload["parts"][0]["sha256"] = _sha256(path)
    _rewrite_manifest(run_dir, payload)


@pytest.mark.parametrize("field", ["seq", "timestamp_ns", "vibration", "temperature"])
def test_a_zero_dimensional_column_is_reported_not_raised(
    run_dir: Path, field: str
) -> None:
    """A 0-d column has no rows to compare, so its length is unknown."""
    if field == "seq":
        _replace_column(run_dir, field, np.array(0, dtype=np.int64))
    else:
        _replace_column(run_dir, field, np.array(35.0))

    result = verify_run(run_dir)  # must not raise IndexError

    assert result.passed is False
    assert "shard.dtype_mismatch" in result.failures
    shard = result.shards[0]
    assert shard.dtype_ok is False
    if field == "seq":
        # No row axis means no row count, and no fabricated boundary values.
        assert shard.observed_rows is None
        assert shard.first_seq is None
        assert shard.last_seq is None
    else:
        # ``seq`` is intact, so the row count is still observable -- but the
        # shard is not a valid table and must not be counted as verified.
        assert shard.observed_rows == 256
    # The other three shards are still inspected.
    assert result.verified_part_count == 3


@pytest.mark.parametrize("field", ["seq", "timestamp_ns", "vibration", "temperature"])
def test_a_two_dimensional_column_is_reported_not_raised(
    run_dir: Path, field: str
) -> None:
    """A 2-d column is the wrong shape for a table column."""
    if field == "seq":
        _replace_column(run_dir, field, np.zeros((16, 16), dtype=np.int64))
    else:
        _replace_column(run_dir, field, np.zeros((16, 16), dtype=np.float64))

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.dtype_mismatch" in result.failures
    assert result.verified_part_count == 3
    if field == "seq":
        assert result.shards[0].observed_rows is None


def test_a_zero_dimensional_column_does_not_stop_later_damage(
    run_dir: Path,
) -> None:
    """A bad dimension in part 0 and a real gap in part 2 must both be reported."""
    _replace_column(run_dir, "timestamp_ns", np.array(0, dtype=np.int64))

    gap_path = run_dir / "raw" / "part-000002.npz"
    with np.load(gap_path, allow_pickle=False) as archive:
        gap = {name: np.asarray(archive[name]) for name in archive.files}
    seq = gap["seq"].copy()
    seq[10] = 999
    gap["seq"] = seq
    np.savez(gap_path, **gap)

    payload = _read_manifest_payload(run_dir)
    payload["parts"][2]["sha256"] = _sha256(gap_path)
    _rewrite_manifest(run_dir, payload)

    result = verify_run(run_dir)

    assert result.passed is False
    assert "shard.dtype_mismatch" in result.failures
    assert "shard.seq_gap" in result.failures
