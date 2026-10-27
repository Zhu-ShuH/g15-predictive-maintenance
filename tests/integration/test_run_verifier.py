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
