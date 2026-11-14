"""Actual raw-data, spectral scaling, and bounded-file reader checks."""

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import zipfile

import numpy as np
import pytest

from g15.console import waveforms
from g15.console.waveforms import WaveformRequestError, read_waveform
from g15.live.schema import FIELDS

RATE = 256
EPOCH = 1_700_000_000_000_000_000


def arrays(start, rows):
    seq = np.arange(start, start + rows, dtype=np.int64)
    return {
        "seq": seq,
        "timestamp_ns": EPOCH + seq * (10**9 // RATE),
        "vibration": 3.0 + 2.0 * np.sin(2 * np.pi * 32 * seq / RATE),
        "temperature": 25.0 + seq.astype(np.float64) / 1000,
    }


def raw_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        np.savez_compressed(stream, **data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def offline(tmp_path, sizes=(256, 256), transform=None):
    directory = tmp_path / "run-1"
    parts, start = [], 0
    for index, size in enumerate(sizes):
        relative = f"raw/part-{index:06d}.npz"
        data = arrays(start, size)
        if transform:
            transform(index, data)
        digest = raw_file(directory / relative, data)
        parts.append(
            {"part_id": index, "path": relative, "rows": size, "sha256": digest}
        )
        start += size
    manifest = {
        "schema_version": "2.0",
        "run_id": "run-1",
        "scenario_id": "scenario-1",
        "device_id": "DEV-001",
        "seed": 42,
        "sample_rate_hz": RATE,
        "start_time_utc": "2023-11-14T22:13:20Z",
        "total_points": start,
        "units": {"vibration": "g", "temperature": "degC"},
        "status": "complete",
        "parts": parts,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory, manifest


def rewrite_manifest(directory, manifest):
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def live(tmp_path, *, captured=768, processed=512):
    directory = tmp_path / "run-1"
    (directory / "artifacts").mkdir(parents=True)
    with sqlite3.connect(directory / "artifacts" / "results.sqlite") as conn:
        conn.execute(
            "CREATE TABLE processed_chunks (run_id TEXT, part_id INTEGER, sha256 TEXT, PRIMARY KEY(run_id,part_id))"
        )
        for start in range(0, captured, 256):
            digest = raw_file(
                directory / f"raw/part-{start // 256:08d}.npz", arrays(start, 256)
            )
            if start < processed:
                conn.execute(
                    "INSERT INTO processed_chunks VALUES (?,?,?)",
                    ("run-1", start // 256, digest),
                )
    descriptor = {
        "schema_version": "1",
        "session_id": "session-1",
        "run_id": "run-1",
        "device_id": "DEV-001",
        "scenario_id": "live-wall-clock-v1",
        "source_kind": "wall_clock_simulator",
        "sample_rate_hz": RATE,
        "window_points": 256,
        "interval_seconds": 1.0,
        "started_at_utc": "2026-11-14T00:00:00Z",
        "updated_at_utc": "2026-11-14T00:00:04Z",
        "status": "running",
        "captured_samples": captured,
        "processed_samples": processed,
        "windows_written": processed // 256,
        "alarms_written": 0,
        "scoring_version": "v1",
        "rule_id": "rule1",
        "error": None,
    }
    assert set(descriptor) == FIELDS
    (directory / "live-session.json").write_text(
        json.dumps(descriptor), encoding="utf-8"
    )
    return directory


def read(tmp_path, **kwargs):
    return read_waveform(tmp_path, "run-1", "DEV-001", **kwargs)


def rejected(tmp_path, code, status=409, **kwargs):
    with pytest.raises(WaveformRequestError) as error:
        read(tmp_path, **kwargs)
    assert (error.value.status, error.value.code) == (status, code)
    assert str(tmp_path) not in str(error.value)


def test_offline_cross_chunk_window_preserves_samples_and_nanoseconds(tmp_path):
    offline(tmp_path)
    result = read(tmp_path, start_seq=240, count=64)
    expected = arrays(240, 64)
    assert result["seq"] == expected["seq"].tolist()
    assert result["vibration"] == expected["vibration"].tolist()
    assert result["temperature"] == expected["temperature"].tolist()
    assert result["timestamp_ns"] == [str(int(x)) for x in expected["timestamp_ns"]]
    assert result["time_seconds"][0] == 0
    assert result["time_seconds"][-1] == 63 / RATE
    assert result["available_points"] == 512
    assert result["start_seq"] == 240
    assert result["end_seq_exclusive"] == 304
    assert result["integrity"] == {"status": "sha256_verified", "source": "manifest"}
    assert result["temperature_role"] == "context_only"
    assert len(json.dumps(result, allow_nan=False)) < 1_000_000


def test_latest_tail_and_partial_final_page(tmp_path):
    offline(tmp_path)
    latest = read(tmp_path, count=64)
    tail = read(tmp_path, start_seq=500, count=64)
    assert latest["seq"] == list(range(448, 512))
    assert tail["count"] == 12
    assert tail["seq"] == list(range(500, 512))
    rejected(tmp_path, "waveform_range_unavailable", 416, start_seq=512)


def test_live_reads_only_committed_and_verifies_persisted_digest(tmp_path):
    directory = live(tmp_path)
    # Captured but uncommitted data is deliberately corrupt: it must be ignored.
    (directory / "raw/part-00000002.npz").write_bytes(b"uncommitted")
    result = read(tmp_path, count=512)
    assert result["available_points"] == 512
    assert result["seq"] == list(range(512))
    assert result["source_kind"] == "wall_clock_simulator"
    assert result["integrity"]["source"] == "processed_chunks"
    rejected(tmp_path, "waveform_range_unavailable", 416, start_seq=512)
    with sqlite3.connect(directory / "artifacts/results.sqlite") as conn:
        conn.execute(
            "UPDATE processed_chunks SET sha256=? WHERE part_id=1", ("0" * 64,)
        )
    rejected(tmp_path, "waveform_integrity_failed")


def test_empty_live_returns_no_invented_waveform_or_integrity_claim(tmp_path):
    live(tmp_path, captured=0, processed=0)
    for kwargs in ({}, {"start_seq": 0}):
        result = read(tmp_path, **kwargs)
        assert result["count"] == 0
        assert result["seq"] == result["temperature"] == result["vibration"] == []
        assert result["spectrum"] is None
        assert result["integrity"]["status"] == "no_samples"
    rejected(tmp_path, "waveform_range_unavailable", 416, start_seq=1)


def test_missing_committed_digest_is_not_treated_as_verified(tmp_path):
    directory = live(tmp_path)
    with sqlite3.connect(directory / "artifacts/results.sqlite") as conn:
        conn.execute("DELETE FROM processed_chunks WHERE part_id=1")
    rejected(tmp_path, "waveform_not_ready", 503)


def test_live_maximum_window_can_cross_seventeen_batches(tmp_path):
    live(tmp_path, captured=4608, processed=4608)
    result = read(tmp_path, start_seq=1, count=4096)
    assert result["seq"] == list(range(1, 4097))
    assert result["spectrum"]["end_seq_exclusive"] == 4097


def test_wal_commits_are_visible_without_mutating_rows(tmp_path):
    directory = live(tmp_path)
    with sqlite3.connect(directory / "artifacts/results.sqlite") as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "UPDATE processed_chunks SET sha256=? WHERE part_id=1", ("f" * 64,)
        )
        writer.commit()
        rejected(tmp_path, "waveform_integrity_failed")
        assert (
            writer.execute(
                "SELECT sha256 FROM processed_chunks WHERE part_id=1"
            ).fetchone()[0]
            == "f" * 64
        )


def test_spectrum_resolves_true_sine_amplitude_and_removes_dc(tmp_path):
    offline(tmp_path, sizes=(1024,))
    result = read(tmp_path, count=1024)
    spectrum = result["spectrum"]
    peak = int(np.argmax(spectrum["amplitude_g"]))
    assert spectrum["frequency_hz"][peak] == 32
    assert spectrum["amplitude_g"][peak] == pytest.approx(2.0, abs=0.001)
    assert spectrum["amplitude_g"][0] < 0.00001
    assert spectrum["units"] == "g"
    assert spectrum["scaling"] == "single_sided_peak_amplitude"
    assert spectrum["window"] == "hann" and spectrum["detrended"]


def test_spectrum_bins_bounded_without_downsampling_raw_data(tmp_path):
    offline(tmp_path, sizes=(4096,))
    result = read(tmp_path, count=4096)
    assert len(result["seq"]) == 4096
    assert len(result["spectrum"]["frequency_hz"]) == 1024
    assert result["spectrum"]["sample_count"] == 2046
    assert result["spectrum"]["start_seq"] == 2050
    assert result["spectrum"]["end_seq_exclusive"] == 4096
    assert read(tmp_path, start_seq=4095, count=2)["spectrum"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"count": 0},
        {"count": 1},
        {"count": True},
        {"count": 4097},
        {"count": 2.0},
        {"start_seq": -1},
        {"start_seq": True},
        {"start_seq": 2**53},
    ],
)
def test_request_bounds_are_checked_before_disk(tmp_path, kwargs):
    rejected(tmp_path, "invalid_waveform_request", 400, **kwargs)


@pytest.mark.parametrize(
    "run_id", ["../run", "C:/run", "run.", "run/child", "run\\child", "a:b", ""]
)
def test_identifier_traversal_rejected(tmp_path, run_id):
    with pytest.raises(WaveformRequestError) as error:
        read_waveform(tmp_path, run_id, "DEV-001")
    assert error.value.status == 400


def test_identity_and_source_ambiguity(tmp_path):
    directory, manifest = offline(tmp_path)
    manifest["device_id"] = "OTHER"
    rewrite_manifest(directory, manifest)
    rejected(tmp_path, "waveform_identity_mismatch")
    (directory / "live-session.json").write_text("{}", encoding="utf-8")
    rejected(tmp_path, "waveform_ambiguous_source")


def test_failed_manifest_not_consumed(tmp_path):
    directory, manifest = offline(tmp_path)
    manifest["status"] = "failed"
    rewrite_manifest(directory, manifest)
    rejected(tmp_path, "waveform_not_ready")


def test_manifest_duplicate_keys_rejected(tmp_path):
    directory, _ = offline(tmp_path)
    path = directory / "manifest.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '"run_id": "run-1"', '"run_id": "run-1", "run_id": "run-1"'
        ),
        encoding="utf-8",
    )
    rejected(tmp_path, "waveform_invalid_data")


def test_manifest_path_escape_and_altered_digest(tmp_path):
    directory, manifest = offline(tmp_path)
    manifest["parts"][0]["path"] = "../outside.npz"
    rewrite_manifest(directory, manifest)
    rejected(tmp_path, "waveform_invalid_data")
    manifest["parts"][0]["path"] = "raw/part-000000.npz"
    rewrite_manifest(directory, manifest)
    with (directory / "raw/part-000000.npz").open("ab") as stream:
        stream.write(b"altered")
    rejected(tmp_path, "waveform_integrity_failed")


@pytest.mark.parametrize(
    "path",
    [
        "raw/part.npz:stream",
        "raw/NUL.npz",
        "raw/part.npz.",
        "private_truth/faults.json",
    ],
)
def test_platform_aliases_and_private_truth_rejected_before_read(
    tmp_path, path, monkeypatch
):
    directory, manifest = offline(tmp_path)
    manifest["parts"][0]["path"] = path
    rewrite_manifest(directory, manifest)
    original = waveforms._read_file

    def guarded(root, candidate, limit):
        assert candidate.name == "manifest.json"
        return original(root, candidate, limit)

    monkeypatch.setattr(waveforms, "_read_file", guarded)
    rejected(tmp_path, "waveform_unsafe_path")


@pytest.mark.parametrize(
    "mutation",
    ["seq", "dtype", "nan", "temperature_inf", "timestamp", "cadence", "extra_field"],
)
def test_invalid_arrays_fail_even_with_matching_sha(tmp_path, mutation):
    def transform(index, data):
        del index
        if mutation == "seq":
            data["seq"][20] += 1
        elif mutation == "dtype":
            data["vibration"] = data["vibration"].astype(np.float32)
        elif mutation == "nan":
            data["vibration"][3] = np.nan
        elif mutation == "temperature_inf":
            data["temperature"][3] = np.inf
        elif mutation == "timestamp":
            data["timestamp_ns"][20] = data["timestamp_ns"][19]
        elif mutation == "cadence":
            data["timestamp_ns"][20:] += 100
        elif mutation == "extra_field":
            data["hidden"] = np.ones(256)

    offline(tmp_path, sizes=(256,), transform=transform)
    rejected(
        tmp_path,
        (
            "waveform_invalid_cadence"
            if mutation == "cadence"
            else "waveform_invalid_data"
        ),
    )


def test_cross_chunk_timestamp_regression_rejected(tmp_path):
    def transform(index, data):
        if index:
            data["timestamp_ns"] -= 10**9

    offline(tmp_path, transform=transform)
    rejected(tmp_path, "waveform_invalid_data")


def test_archive_count_and_byte_budgets(tmp_path, monkeypatch):
    offline(tmp_path, sizes=(16,) * 33)
    rejected(tmp_path, "waveform_budget_exceeded", 413)
    monkeypatch.setattr(waveforms, "MAX_PARTS", 40)
    monkeypatch.setattr(waveforms, "MAX_ARCHIVE_BYTES", 20)
    rejected(tmp_path, "waveform_budget_exceeded", 413)


def test_declared_uncompressed_budget_blocks_before_loading(tmp_path, monkeypatch):
    directory, manifest = offline(tmp_path, sizes=(256,))
    manifest["parts"][0]["rows"] = 2_000_000
    manifest["total_points"] = 2_000_000
    rewrite_manifest(directory, manifest)
    monkeypatch.setattr(waveforms, "_chunk", lambda *args: pytest.fail("must not load"))
    rejected(tmp_path, "waveform_budget_exceeded", 413)


def test_forged_npy_huge_shape_and_huge_header_are_rejected(tmp_path):
    directory, manifest = offline(tmp_path, sizes=(256,))
    path = directory / manifest["parts"][0]["path"]
    original = path.read_bytes()
    for variant in ("shape", "header"):
        with zipfile.ZipFile(io.BytesIO(original)) as source:
            members = {name: source.read(name) for name in source.namelist()}
        if variant == "shape":
            members["vibration.npy"] = members["vibration.npy"].replace(
                b"(256,)", b"(999,)"
            )
        else:
            members["vibration.npy"] = (
                b"\x93NUMPY\x02\x00" + struct.pack("<I", 2**32 - 1) + b"x" * 2164
            )
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name, value in members.items():
                target.writestr(name, value)
        manifest["parts"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        rewrite_manifest(directory, manifest)
        rejected(
            tmp_path,
            (
                "waveform_budget_exceeded"
                if variant == "header"
                else "waveform_invalid_data"
            ),
            413 if variant == "header" else 409,
        )


def test_duplicate_zip_members_are_not_accepted(tmp_path):
    directory, manifest = offline(tmp_path, sizes=(256,))
    path = directory / manifest["parts"][0]["path"]
    with zipfile.ZipFile(path) as source:
        content = source.read("seq.npy")
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(path, "a") as target:
            target.writestr("seq.npy", content)
    manifest["parts"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    rewrite_manifest(directory, manifest)
    rejected(tmp_path, "waveform_invalid_data")


def test_ordinary_large_offline_chunk_is_supported(tmp_path):
    offline(tmp_path, sizes=(102400,))
    result = read(tmp_path, start_seq=50000, count=1024)
    assert result["seq"] == list(range(50000, 51024))
    assert result["available_points"] == 102400


def test_symlink_file_or_ancestor_rejected(tmp_path):
    directory, _ = offline(tmp_path)
    original = directory / "raw/part-000000.npz"
    target = tmp_path / "outside.npz"
    original.replace(target)
    try:
        original.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"OS does not permit symlink creation: {exc}")
    rejected(tmp_path, "waveform_unsafe_path")


def test_reparse_attribute_rejected_even_without_os_link_privilege(
    tmp_path, monkeypatch
):
    directory, _ = offline(tmp_path)
    original = Path.lstat

    class Reparse:
        st_mode = 0o040755
        st_file_attributes = 0x400

    def altered(path, *args, **kwargs):
        return (
            Reparse() if path == directory / "raw" else original(path, *args, **kwargs)
        )

    monkeypatch.setattr(Path, "lstat", altered)
    rejected(tmp_path, "waveform_unsafe_path")


def test_private_truth_never_read(tmp_path, monkeypatch):
    directory, _ = offline(tmp_path)
    private = directory / "private_truth"
    private.mkdir()
    (private / "faults.json").write_text("not valid", encoding="utf-8")
    original = waveforms._read_file

    def guarded(root, path, limit):
        assert "private_truth" not in path.parts
        return original(root, path, limit)

    monkeypatch.setattr(waveforms, "_read_file", guarded)
    assert read(tmp_path)["count"] == 512
