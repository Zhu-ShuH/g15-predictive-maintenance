"""Real wall-clock capture, transaction boundaries and failure-preserving stop."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

import numpy as np
import pytest

from g15.live import LiveConfig, LiveRuntime
from g15.live.schema import atomic_json, load_live_session
from g15.storage.repository import ConsumptionRepository, decode_state_json

RULES = Path(__file__).resolve().parents[2] / "configs" / "health_rules.yaml"


def _config(**changes):
    base = LiveConfig(
        devices=1,
        sample_rate_hz=160,
        window_points=16,
        interval_seconds=0.1,
        max_duration_seconds=1.0,
        cycle_windows=4,
    )
    return replace(base, **changes)


def _runtime(tmp_path, **changes):
    return LiveRuntime(tmp_path, config=_config(**changes), health_rules_path=RULES)


def _wait_for(runtime, predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = runtime.status_snapshot()
        if predicate(status):
            return status
        if status["status"] == "error":
            pytest.fail(f"unexpected runtime error: {status}")
        time.sleep(0.005)
    pytest.fail(f"runtime did not reach requested state: {runtime.status_snapshot()}")


def _db(runtime, device):
    return sqlite3.connect(
        runtime.runs_root / device["run_id"] / "artifacts" / "results.sqlite"
    )


def test_capture_pause_is_per_device_drains_and_resumes_without_sequence_reset(
    tmp_path,
):
    runtime = _runtime(tmp_path, devices=2, max_duration_seconds=8)
    runtime.start()
    try:
        first = _wait_for(runtime, lambda s: s["devices"][0]["processed_samples"] >= 16)
        pair = tuple(first["devices"][0][key] for key in ("run_id", "device_id"))
        runtime.control_capture("pause", pair)
        paused = runtime.status_snapshot()
        captured = paused["devices"][0]["captured_samples"]
        other = paused["devices"][1]["captured_samples"]
        drained = _wait_for(
            runtime,
            lambda s: s["devices"][0]["processed_samples"] == captured
            and s["devices"][1]["captured_samples"] >= other + 32,
        )
        assert drained["devices"][0]["captured_samples"] == captured
        assert drained["devices"][0]["capture_paused"] is True
        assert drained["devices"][1]["capture_paused"] is False
        runtime.control_capture("resume", pair)
        resumed = _wait_for(
            runtime, lambda s: s["devices"][0]["processed_samples"] > captured
        )
        assert resumed["devices"][0]["run_id"] == pair[0]
        assert resumed["devices"][0]["capture_paused"] is False
        runtime.control_capture("pause")
        frozen = runtime.status_snapshot()
        time.sleep(0.25)
        assert [
            d["captured_samples"] for d in runtime.status_snapshot()["devices"]
        ] == [d["captured_samples"] for d in frozen["devices"]]
        runtime.control_capture("resume")
        assert all(
            not d["capture_paused"] for d in runtime.status_snapshot()["devices"]
        )
    finally:
        final = runtime.stop()
    for device in final["devices"]:
        assert device["captured_samples"] == device["processed_samples"]
        parts = sorted((runtime.runs_root / device["run_id"] / "raw").glob("*.npz"))
        sequences = []
        for path in parts:
            with np.load(path, allow_pickle=False) as data:
                sequences.extend(data["seq"].tolist())
        assert sequences == list(range(device["captured_samples"]))
    with pytest.raises(RuntimeError):
        runtime.control_capture("resume")


def test_paused_capture_still_obeys_wall_clock_limit_and_validates_actions(tmp_path):
    runtime = _runtime(tmp_path, max_duration_seconds=0.4)
    with pytest.raises(RuntimeError):
        runtime.control_capture("pause")
    runtime.start()
    try:
        with pytest.raises(ValueError):
            runtime.control_capture("reset")
        with pytest.raises(ValueError):
            runtime.control_capture("pause", ("missing", "missing"))
        runtime.control_capture("pause")
        assert runtime.wait(5)
        assert runtime.status_snapshot()["status"] == "limit_reached"
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    "changes",
    [
        {"devices": 11},
        {"devices": True},
        {"devices": 1.5},
        {"queue_capacity": 0},
        {"max_duration_seconds": float("nan")},
        {"interval_seconds": float("inf")},
        {"sample_rate_hz": 160, "window_points": 17},
        {"max_disk_bytes": 1024},
        {"backpressure_timeout_seconds": 31},
    ],
)
def test_live_config_rejects_unbounded_or_incoherent_values(changes):
    with pytest.raises(ValueError):
        _config(**changes)


def test_live_samples_are_paced_then_committed_and_reported_without_truth(tmp_path):
    runtime = _runtime(tmp_path, devices=2, max_duration_seconds=0.85)
    assert len(runtime.catalog()) == 2
    assert not list(runtime.runs_root.glob("*/raw/*.npz"))
    began = time.monotonic()
    runtime.start()
    try:
        time.sleep(0.025)
        assert all(
            row["captured_samples"] == 0 for row in runtime.status_snapshot()["devices"]
        )
        assert runtime.wait(10)
    finally:
        runtime.stop()
    status = runtime.status_snapshot()
    assert time.monotonic() - began >= 0.8
    assert status["status"] == "limit_reached"
    assert status["limit_reason"] == "duration_limit"
    assert status["error"] is None
    assert status["resources"]["latest"]["process_id"] == os.getpid()
    assert status["resources"]["observed_max"]["rss_bytes"] > 0
    assert status["latency"]["recent_p95_seconds"] > 0
    assert not list(runtime.session_root.rglob("*private_truth*"))
    assert not list(runtime.runs_root.glob("*/manifest.json"))
    for device in status["devices"]:
        captured = device["captured_samples"]
        assert 4 * 16 <= captured <= 9 * 16
        assert captured == device["processed_samples"]
        assert captured == device["windows_written"] * 16
        assert device["queue_depth"] == device["pending_samples"] == 0
        directory = runtime.runs_root / device["run_id"]
        descriptor = load_live_session(directory, device["run_id"], device["device_id"])
        assert descriptor["processed_samples"] == captured
        seq = []
        for file in sorted((directory / "raw").glob("*.npz")):
            with np.load(file, allow_pickle=False) as raw:
                seq.extend(raw["seq"].tolist())
        assert seq == list(range(captured))
        with _db(runtime, device) as connection:
            assert connection.execute("SELECT status FROM runs").fetchone() == (
                "streaming",
            )
            rows = connection.execute(
                "SELECT start_seq,end_seq_exclusive,health_score FROM feature_windows ORDER BY start_seq"
            ).fetchall()
            assert [row[:2] for row in rows] == [
                (n, n + 16) for n in range(0, captured, 16)
            ]
            # A real abnormal phase is scored, followed by real normal recovery.
            assert min(row[2] for row in rows) < 40
            assert max(row[2] for row in rows) > 70
            assert connection.execute("SELECT COUNT(*) FROM alarms").fetchone()[0] > 0
            assert connection.execute(
                "SELECT COUNT(*) FROM processed_chunks"
            ).fetchone()[0] == len(rows)
        report = json.loads((directory / "reports" / "summary.json").read_text())
        assert report["run_id"] == device["run_id"]
        assert report["processed_samples"] == captured
        assert report["feature_windows"] == device["windows_written"]
        assert report["alarm_count"] == device["alarms_written"]
        assert report["health_score_min"] == min(row[2] for row in rows)
        assert report["dropped_points"] == 0
        assert report["fault_events"] is report["detected_events"] is None
        assert report["verifiable"] is False
        assert report["passed"] is report["detection_rate"] is None
    records = [
        json.loads(line)
        for line in (runtime.session_root / "latency.ndjson").read_text().splitlines()
    ]
    assert len(records) == sum(row["windows_written"] for row in status["devices"])
    for row in records:
        assert (
            row["capture_started_monotonic_ns"]
            <= row["analysis_started_monotonic_ns"]
            <= row["committed_monotonic_ns"]
        )
        assert row["capture_to_commit_seconds"] > 0
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        runtime.start()


def test_graceful_stop_drains_already_captured_queue(tmp_path, monkeypatch):
    original = LiveRuntime._process_task
    entered = threading.Event()
    release = threading.Event()

    def delayed(self, *args):
        entered.set()
        assert release.wait(4)
        return original(self, *args)

    monkeypatch.setattr(LiveRuntime, "_process_task", delayed)
    runtime = _runtime(tmp_path, queue_capacity=2, max_duration_seconds=10).start()
    try:
        assert entered.wait(3)
        _wait_for(runtime, lambda value: value["devices"][0]["queue_depth"] >= 1)
        pending = runtime.stop(timeout=0.01)
        assert pending["status"] == "stopping"
        assert pending["devices"][0]["pending_samples"] > 0
        release.set()
        assert runtime.wait(5)
        final = runtime.stop()
        # A timed-out caller is reported; draining never discards captured work.
        assert final["status"] == "error"
        assert final["error"] == "stop_timeout"
        device = final["devices"][0]
        assert device["captured_samples"] == device["processed_samples"]
        assert device["queue_depth"] == 0
    finally:
        release.set()
        runtime.stop()


def test_explicit_stop_recovers_alarm_state_across_batches(tmp_path):
    runtime = _runtime(tmp_path, max_duration_seconds=10).start()
    try:
        status = _wait_for(
            runtime, lambda value: value["devices"][0]["windows_written"] >= 4
        )
        final = runtime.stop()
        assert final["status"] == "stopped"
        with _db(runtime, status["devices"][0]) as connection:
            state = decode_state_json(
                connection.execute("SELECT state_json FROM alarm_states").fetchone()[0]
            )
            assert state.last_window_start_seq >= 48
            assert state.recovered_at_seq is not None
    finally:
        runtime.stop()


def test_analysis_failure_rolls_back_whole_batch_and_preserves_raw(
    tmp_path, monkeypatch
):
    original = ConsumptionRepository.insert_feature_window

    def fail_after_insert(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("private-path-token-must-not-escape")

    monkeypatch.setattr(
        ConsumptionRepository, "insert_feature_window", fail_after_insert
    )
    runtime = _runtime(tmp_path, max_duration_seconds=10).start()
    try:
        assert runtime.wait(5)
        final = runtime.stop()
        assert final["status"] == "error"
        assert "private-path-token" not in json.dumps(final)
        device = final["devices"][0]
        assert device["captured_samples"] >= 16
        assert device["processed_samples"] == 0
        assert list((runtime.runs_root / device["run_id"] / "raw").glob("*.npz"))
        with _db(runtime, device) as connection:
            for table in (
                "feature_windows",
                "alarms",
                "alarm_states",
                "processed_chunks",
            ):
                assert (
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    == 0
                )
        errors = (runtime.session_root / "runtime-errors.ndjson").read_text()
        assert "RuntimeError" in errors
        assert "private-path-token" not in errors
    finally:
        runtime.stop()


def test_backpressure_is_bounded_and_failure_still_drains_raw(tmp_path, monkeypatch):
    original = LiveRuntime._process_task

    def slow(self, *args):
        time.sleep(0.25)
        return original(self, *args)

    monkeypatch.setattr(LiveRuntime, "_process_task", slow)
    runtime = _runtime(
        tmp_path,
        devices=2,
        queue_capacity=1,
        max_duration_seconds=10,
        backpressure_timeout_seconds=0.03,
    ).start()
    try:
        assert runtime.wait(6)
        final = runtime.stop()
        assert final["status"] == "error"
        assert final["backpressure_events"] > 0
        assert final["error"] == "backpressure_timeout"
        for device in final["devices"]:
            assert device["captured_samples"] == device["processed_samples"]
            assert device["queue_depth"] == 0
    finally:
        runtime.stop()


def test_output_lock_excludes_second_writer_and_new_session_does_not_reset(tmp_path):
    first = _runtime(tmp_path, max_duration_seconds=10).start()
    second = _runtime(tmp_path)
    try:
        assert second.session_id != first.session_id
        with pytest.raises(RuntimeError, match="another live writer"):
            second.start()
        assert first.status_snapshot()["status"] == "running"
    finally:
        first.stop()
    second.start()
    assert second.wait(5)
    assert first.session_root.exists() and second.session_root.exists()
    stopped = _runtime(tmp_path)
    assert stopped.stop()["status"] == "stopped"
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        stopped.start()


def test_disk_limit_stops_before_capturing_more_data(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        runtime, "_disk_usage_bytes", lambda: runtime.config.max_disk_bytes
    )
    runtime.start()
    assert runtime.wait(4)
    assert runtime.status_snapshot()["limit_reason"] == "disk_limit"
    assert runtime.status_snapshot()["status"] == "limit_reached"
    assert runtime.status_snapshot()["devices"][0]["captured_samples"] == 0


@pytest.mark.parametrize("rename_when", ["before_stat", "after_stat"])
def test_disk_snapshot_tolerates_a_real_atomic_publication(
    tmp_path, monkeypatch, rename_when
):
    runtime = _runtime(tmp_path, max_duration_seconds=0.45)
    temporary = runtime.session_root / "publication-probe.json.tmp-test"
    published = runtime.session_root / "publication-probe.json"
    temporary.write_text('{"version":2}', encoding="utf-8")
    published.write_text('{"version":1}', encoding="utf-8")
    original_stat = Path.stat
    renamed = threading.Event()

    def stat_during_publication(path, *args, **kwargs):
        if path == temporary:
            if not renamed.is_set():
                if rename_when == "before_stat":
                    # The directory entry was enumerated, then its pathname
                    # vanished before this snapshot could inspect it.
                    os.replace(temporary, published)
                    renamed.set()
                    return original_stat(path, *args, **kwargs)
                observed = original_stat(path, *args, **kwargs)
                # This is the previous is_file()/stat() failure: a valid first
                # stat races a real publisher before a second stat could run.
                os.replace(temporary, published)
                renamed.set()
                return observed
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_during_publication)
    runtime.start()
    try:
        assert runtime.wait(5)
        final = runtime.stop()
        assert renamed.is_set()
        assert final["status"] == "limit_reached"
        assert final["error"] is None
        device = final["devices"][0]
        assert device["captured_samples"] >= 16
        assert device["captured_samples"] == device["processed_samples"]
        assert device["pending_samples"] == device["queue_depth"] == 0
        assert json.loads(published.read_text(encoding="utf-8")) == {"version": 2}
    finally:
        runtime.stop()


@pytest.mark.parametrize("failure", [PermissionError, OSError])
def test_disk_snapshot_does_not_hide_real_io_failures(tmp_path, monkeypatch, failure):
    runtime = _runtime(tmp_path, max_duration_seconds=1)
    blocked = runtime.session_root / "unreadable-probe.json"
    blocked.write_text("{}", encoding="utf-8")
    original_stat = Path.stat

    def denied_stat(path, *args, **kwargs):
        if path == blocked:
            raise failure("diagnostic-private-detail")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    runtime.start()
    try:
        assert runtime.wait(5)
        final = runtime.stop()
        assert final["status"] == "error"
        assert final["error"] == "capture_error"
        assert final["devices"][0]["captured_samples"] == 0
        errors = (runtime.session_root / "runtime-errors.ndjson").read_text()
        assert failure.__name__ in errors
        assert "diagnostic-private-detail" not in errors
    finally:
        runtime.stop()


@pytest.mark.parametrize("directory_name", ["artifacts", "reports"])
@pytest.mark.parametrize("failure", [PermissionError, OSError, FileNotFoundError])
def test_disk_snapshot_does_not_hide_directory_scan_failures(
    tmp_path, monkeypatch, directory_name, failure
):
    runtime = _runtime(tmp_path, max_duration_seconds=0.4)
    blocked = runtime.runs_root / runtime.catalog()[0]["run_id"] / directory_name
    original_scandir, original_listdir = os.scandir, os.listdir
    scans = []

    def scan_or_fail(original, path, *args, **kwargs):
        if (
            isinstance(path, (str, bytes, os.PathLike))
            and Path(os.fsdecode(path)) == blocked
        ):
            scans.append(path)
            raise failure("diagnostic-private-directory-detail")
        return original(path, *args, **kwargs)

    # Inject below pathlib: Python 3.12 glob silently swallows scandir OSError,
    # while iterdir uses listdir and must let the real directory failure escape.
    monkeypatch.setattr(
        os,
        "scandir",
        lambda path=".", *args, **kwargs: scan_or_fail(
            original_scandir, path, *args, **kwargs
        ),
    )
    monkeypatch.setattr(
        os,
        "listdir",
        lambda path=".", *args, **kwargs: scan_or_fail(
            original_listdir, path, *args, **kwargs
        ),
    )
    runtime.start()
    try:
        assert runtime.wait(5)
        final = runtime.stop()
        assert scans
        assert final["status"] == "error"
        assert final["error"] == "capture_error"
        assert final["devices"][0]["captured_samples"] == 0
        errors = (runtime.session_root / "runtime-errors.ndjson").read_text()
        assert failure.__name__ in errors
        assert "diagnostic-private-directory-detail" not in errors
    finally:
        runtime.stop()


def test_descriptor_readers_remain_coherent_during_real_updates(tmp_path):
    runtime = _runtime(tmp_path, max_duration_seconds=0.8).start()
    device = runtime.catalog()[0]
    reads = 0
    try:
        while not runtime.wait(0.001):
            value = load_live_session(
                runtime.runs_root / device["run_id"],
                device["run_id"],
                device["device_id"],
            )
            assert value["processed_samples"] <= value["captured_samples"]
            reads += 1
        assert reads > 5
        assert runtime.status_snapshot()["status"] == "limit_reached"
    finally:
        runtime.stop()


@pytest.fixture
def descriptor(tmp_path):
    runtime = _runtime(tmp_path)
    resource = runtime.catalog()[0]
    path = runtime.runs_root / resource["run_id"]
    return path, resource, json.loads((path / "live-session.json").read_text())


@pytest.mark.parametrize(
    "changes",
    [
        {"device_id": "other"},
        {"status": []},
        {"sample_rate_hz": True},
        {"interval_seconds": float("nan")},
        {"processed_samples": 16},
        {"extra": "field"},
        {"source_kind": "precomputed"},
        {"started_at_utc": "2026-01-01T00:00:00"},
    ],
)
def test_live_schema_rejects_malformed_or_incoherent_descriptor(descriptor, changes):
    directory, resource, value = descriptor
    value.update(changes)
    (directory / "live-session.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_live_session(directory, resource["run_id"], resource["device_id"])


def test_live_schema_rejects_duplicate_and_manifest_ambiguity(descriptor):
    directory, resource, value = descriptor
    path = directory / "live-session.json"
    path.write_text(json.dumps(value)[:-1] + ',"status":"stopped"}')
    with pytest.raises(ValueError, match="duplicate"):
        load_live_session(directory, resource["run_id"], resource["device_id"])
    atomic_json(path, value)
    (directory / "manifest.json").write_text("{}")
    with pytest.raises(ValueError, match="ambiguity"):
        load_live_session(directory, resource["run_id"], resource["device_id"])


def test_live_schema_rejects_nonregular_file_before_open(descriptor):
    directory, resource, _value = descriptor
    path = directory / "live-session.json"
    path.unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(path)
    else:
        path.mkdir()
    began = time.monotonic()
    with pytest.raises(ValueError, match="file type"):
        load_live_session(directory, resource["run_id"], resource["device_id"])
    assert time.monotonic() - began < 0.5
