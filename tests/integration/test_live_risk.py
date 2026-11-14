"""Live commands must alter durable samples and the unchanged analysis pipeline."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

import numpy as np
import pytest

from g15.analysis.features import extract_features
from g15.analysis.health import score_health
from g15.live import LiveConfig, LiveRuntime
from g15.live.risk import LiveRiskError
from g15.simulation.engine import simulate_chunk

RULES = Path(__file__).resolve().parents[2] / "configs" / "health_rules.yaml"


@pytest.fixture
def runtime(tmp_path):
    value = LiveRuntime(
        tmp_path,
        config=LiveConfig(
            devices=2,
            sample_rate_hz=640,
            window_points=64,
            interval_seconds=0.1,
            max_duration_seconds=None,
            cycle_windows=4,
        ),
        health_rules_path=RULES,
    ).start()
    value.control_capture("pause")
    yield value
    final = value.stop()
    assert final["status"] == "stopped"
    assert all(
        d["captured_samples"] == d["processed_samples"] for d in final["devices"]
    )


def _request(runtime, action="inject", index=0, **changes):
    device = runtime.catalog()[index]
    result = {
        "session_id": runtime.session_id,
        "run_id": device["run_id"],
        "device_id": device["device_id"],
        "request_id": uuid.uuid4().hex,
        "action": action,
    }
    if action == "inject":
        result.update(
            fault_type="step", amplitude=10, delay_seconds=0.15, duration_seconds=0.35
        )
    result.update(changes)
    return result


def _wait(runtime, predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = runtime.status_snapshot()
        if predicate(value):
            return value
        assert value["status"] != "error", value
        time.sleep(0.005)
    pytest.fail(f"Live state did not arrive: {runtime.status_snapshot()}")


def _raw(runtime, index):
    device = runtime.catalog()[index]
    result = {key: [] for key in ("seq", "timestamp_ns", "vibration", "temperature")}
    for path in sorted((runtime.runs_root / device["run_id"] / "raw").glob("*.npz")):
        with np.load(path, allow_pickle=False) as part:
            for key in result:
                result[key].append(part[key])
    return {key: np.concatenate(parts) for key, parts in result.items()}


def _baseline(runtime, index, count, mode="normal"):
    config = json.loads((runtime.session_root / "capture-config.json").read_text())
    started = runtime.status_snapshot()["started_at_utc"]
    result = {key: [] for key in ("seq", "timestamp_ns", "vibration", "temperature")}
    for start in range(0, count, runtime.config.window_points):
        phase = (
            (start // runtime.config.window_points + index)
            % runtime.config.cycle_windows
        ) / runtime.config.cycle_windows
        if mode == "normal" or phase < 0.25 or phase >= 0.75:
            baseline, noise = 0.5, 0.05
        elif phase < 0.5:
            baseline, noise = 0.5 + (phase - 0.25) * 4, 0.05 + (phase - 0.25) * 0.4
        else:
            baseline, noise = 4.5, 1.2
        params = {
            "scenario": {
                "seed": config["config"]["seed"] + index,
                "sample_rate_hz": runtime.config.sample_rate_hz,
                "start_time_utc": started,
                "device_id": runtime.catalog()[index]["device_id"],
                "total_points": count,
            },
            "signals": {
                "vibration": {
                    "baseline": baseline,
                    "noise_std": noise,
                    "degradation_rate_per_sample": 0,
                    "unit": "g",
                },
                "temperature": {
                    "baseline": 25,
                    "noise_std": 0.1,
                    "degradation_rate_per_sample": 0,
                    "unit": "degC",
                },
            },
        }
        chunk = simulate_chunk(params, start, runtime.config.window_points)
        for key in result:
            result[key].append(getattr(chunk, key))
    return {key: np.concatenate(parts) for key, parts in result.items()}


@pytest.mark.parametrize("fault_type", ["spike", "step", "burst"])
def test_fault_changes_real_samples_across_batches_and_real_scores_only_on_target(
    runtime, fault_type
):
    anchor = runtime.status_snapshot()["devices"][0]["captured_samples"]
    accepted = runtime.control_risk(_request(runtime, fault_type=fault_type))
    event = accepted["devices"][0]["event"]
    assert event["start_seq"] == anchor + 96
    assert event["duration_points"] == 224
    assert accepted["devices"][0]["mode"] == "normal"
    assert accepted["devices"][1]["mode"] == "auto"
    runtime.control_capture("resume")
    _wait(
        runtime,
        lambda s: s["devices"][0]["processed_samples"]
        >= event["end_seq_exclusive"] + 128,
    )
    runtime.control_capture("pause")
    runtime.stop()
    snapshot = runtime.risk_snapshot()
    assert snapshot["devices"][0]["event"]["status"] == "completed"
    assert snapshot["devices"][0]["mode"] == "normal"
    raw = _raw(runtime, 0)
    normal = _baseline(runtime, 0, len(raw["seq"]))
    perturbation = np.zeros(len(raw["seq"]))
    k = np.arange(event["duration_points"], dtype=float)
    if fault_type == "step":
        signal = np.full(len(k), 10.0)
    elif fault_type == "spike":
        signal = 10 * np.exp(-k / (len(k) / 4))
    else:
        signal = (
            10
            * np.sin(2 * np.pi * k / 8)
            * (0.5 - 0.5 * np.cos(2 * np.pi * k / (len(k) - 1)))
        )
    perturbation[event["start_seq"] : event["end_seq_exclusive"]] = signal
    np.testing.assert_allclose(
        raw["vibration"][anchor:],
        normal["vibration"][anchor:] + perturbation[anchor:],
        rtol=1e-12,
        atol=1e-12,
    )
    for key in ("seq", "timestamp_ns", "temperature"):
        np.testing.assert_array_equal(raw[key], normal[key])
    other = _raw(runtime, 1)
    automatic = _baseline(runtime, 1, len(other["seq"]), mode="auto")
    for key in other:
        np.testing.assert_array_equal(other[key], automatic[key])
    device = runtime.catalog()[0]
    with sqlite3.connect(
        runtime.runs_root / device["run_id"] / "artifacts" / "results.sqlite"
    ) as db:
        scores = db.execute(
            "SELECT start_seq, health_score FROM feature_windows ORDER BY start_seq"
        ).fetchall()
        alarms = db.execute("SELECT start_seq FROM alarms").fetchall()
    for start, score in scores:
        actual = score_health(
            extract_features(
                raw["vibration"][start : start + 64], 640, window_start_seq=start
            ),
            runtime.rules,
        )
        assert score == actual.health_score
    assert any(
        event["start_seq"] - 64 < start < event["end_seq_exclusive"]
        for (start,) in alarms
    )
    assert (
        min(
            score
            for start, score in scores
            if event["start_seq"] <= start < event["end_seq_exclusive"]
        )
        < 70
    )
    assert (
        min(score for start, score in scores if start >= event["end_seq_exclusive"])
        > 90
    )
    persisted = json.loads((runtime.session_root / "risk-status.json").read_text())
    assert persisted["devices"][0]["event"]["status"] == "completed"
    assert not list(runtime.session_root.rglob("*private_truth*"))


def test_pause_freezes_schedule_and_manual_stop_cancels_uncaptured_risk(runtime):
    request = _request(runtime, delay_seconds=0.3)
    first = runtime.control_risk(request)
    second = first["devices"][1]
    runtime.control_capture("resume", (second["run_id"], second["device_id"]))
    _wait(runtime, lambda s: s["devices"][1]["processed_samples"] >= 256)
    event = runtime.risk_snapshot()["devices"][0]["event"]
    assert event["status"] == "scheduled"
    assert event["captured_points"] == event["processed_points"] == 0
    assert event["start_seq"] == first["devices"][0]["event"]["start_seq"]
    runtime.stop()
    terminal = runtime.risk_snapshot()
    assert terminal["devices"][0]["event"]["status"] == "cancelled"
    assert terminal["devices"][0]["event"]["cancel_reason"] == "session_stopped"
    assert terminal["controls_available"] is False
    assert runtime.control_risk(request)["request"]["replayed"] is True
    with pytest.raises(LiveRiskError, match="not running"):
        runtime.control_risk(_request(runtime, "automatic"))


def test_clear_preserves_captured_samples_then_returns_normal_until_automatic(runtime):
    first = runtime.control_risk(_request(runtime, delay_seconds=0, duration_seconds=3))
    start = first["devices"][0]["event"]["start_seq"]
    runtime.control_capture("resume")
    _wait(runtime, lambda s: s["devices"][0]["processed_samples"] >= start + 64)
    runtime.control_capture("pause")
    captured = runtime.status_snapshot()["devices"][0]["captured_samples"]
    original = _raw(runtime, 0)["vibration"].copy()
    cleared = runtime.control_risk(_request(runtime, "clear"))
    assert cleared["devices"][0]["event"]["cancel_seq"] == captured
    assert cleared["devices"][0]["event"]["cancel_reason"] == "cleared"
    runtime.control_capture("resume")
    _wait(runtime, lambda s: s["devices"][0]["processed_samples"] >= captured + 128)
    runtime.control_capture("pause")
    raw = _raw(runtime, 0)
    normal = _baseline(runtime, 0, len(raw["seq"]))
    np.testing.assert_array_equal(raw["vibration"][:captured], original)
    np.testing.assert_array_equal(
        raw["vibration"][captured:], normal["vibration"][captured:]
    )
    auto_start = len(raw["seq"])
    assert (
        runtime.control_risk(_request(runtime, "automatic"))["devices"][0]["mode"]
        == "auto"
    )
    runtime.control_capture("resume")
    _wait(runtime, lambda s: s["devices"][0]["processed_samples"] >= auto_start + 256)
    runtime.control_capture("pause")
    raw = _raw(runtime, 0)
    automatic = _baseline(runtime, 0, len(raw["seq"]), mode="auto")
    np.testing.assert_array_equal(
        raw["vibration"][auto_start:], automatic["vibration"][auto_start:]
    )


@pytest.mark.parametrize("action", [None, "clear", "automatic"])
def test_progress_waits_for_commit_and_mode_changes_preserve_full_capture(
    runtime, monkeypatch, action
):
    release, entered = threading.Event(), threading.Event()
    process = runtime._process_task

    def held(task, repository, state):
        entered.set()
        assert release.wait(5)
        return process(task, repository, state)

    monkeypatch.setattr(runtime, "_process_task", held)
    result = runtime.control_risk(
        _request(runtime, delay_seconds=0, duration_seconds=0.2)
    )
    event = result["devices"][0]["event"]
    try:
        runtime.control_capture("resume")
        assert entered.wait(3)
        _wait(
            runtime,
            lambda s: s["devices"][0]["captured_samples"] >= event["end_seq_exclusive"],
        )
        runtime.control_capture("pause")
        recovery = runtime.risk_snapshot()["devices"][0]["event"]
        assert recovery["status"] == "recovering"
        assert recovery["captured_points"] == event["duration_points"]
        assert recovery["processed_points"] == 0
        if action is not None:
            changed = runtime.control_risk(_request(runtime, action))
            device = changed["devices"][0]
            assert device["mode"] == ("auto" if action == "automatic" else "normal")
            assert device["event"]["status"] == "recovering"
            for key in ("cancel_seq", "cancelled_at_utc", "cancel_reason"):
                assert device["event"][key] is None
            ledger = json.loads((runtime.session_root / "risk-ledger.json").read_text())
            assert ledger["events"][event["event_id"]]["cancel_seq"] is None
    finally:
        release.set()
    _wait(
        runtime,
        lambda s: s["devices"][0]["processed_samples"] >= event["end_seq_exclusive"],
    )
    completed = runtime.risk_snapshot()["devices"][0]["event"]
    assert completed["status"] == "completed"
    assert completed["processed_points"] == event["duration_points"]
    assert completed["cancel_reason"] is None


def test_commands_are_idempotent_bound_to_session_device_and_persist_before_effect(
    runtime, monkeypatch
):
    import g15.live.risk as module

    request = _request(runtime)
    before = runtime.risk_snapshot()
    ledger = runtime.session_root / "risk-ledger.json"
    original = ledger.read_bytes()

    def fail_save(*_args):
        raise OSError("private path must not escape")

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_json", fail_save)
        with pytest.raises(LiveRiskError) as raised:
            runtime.control_risk(request)
        assert (raised.value.status, raised.value.code) == (
            503,
            "live_risk_persistence_failed",
        )
        assert "private" not in str(raised.value)
        assert runtime.risk_snapshot() == before
        assert ledger.read_bytes() == original
    accepted = runtime.control_risk(request)
    repeated = runtime.control_risk(dict(request))
    assert repeated["request"]["replayed"] is True
    assert repeated["version"] == accepted["version"] == 1
    assert repeated["devices"] == accepted["devices"]
    with pytest.raises(LiveRiskError) as raised:
        runtime.control_risk({**request, "amplitude": 9})
    assert raised.value.code == "live_risk_request_conflict"
    with pytest.raises(LiveRiskError) as raised:
        runtime.control_risk(_request(runtime))
    assert raised.value.code == "live_risk_busy"
    with pytest.raises(LiveRiskError) as raised:
        runtime.control_risk(_request(runtime, session_id="live-old"))
    assert raised.value.code == "live_risk_session_changed"
    with pytest.raises(LiveRiskError) as raised:
        runtime.control_risk(_request(runtime, device_id="LIVE-002"))
    assert (raised.value.status, raised.value.code) == (
        404,
        "live_risk_device_not_found",
    )
    recorded = json.loads(ledger.read_text())
    assert recorded["requests"][request["request_id"]]["body"] == request
    assert recorded["version"] == 1
    assert len(recorded["events"]) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"amplitude": 0},
        {"amplitude": 11},
        {"amplitude": True},
        {"amplitude": float("nan")},
        {"delay_seconds": -1},
        {"delay_seconds": float("inf")},
        {"delay_seconds": 301},
        {"duration_seconds": 0},
        {"duration_seconds": 301},
        {"amplitude": 10**1000},
        {"fault_type": "unknown"},
        {"fault_type": "burst", "duration_seconds": 1 / 640},
        {"request_id": "bad/id"},
        {"extra": 1},
    ],
)
def test_invalid_plans_do_not_change_modes_or_audit(runtime, changes):
    original = runtime.risk_snapshot()
    with pytest.raises(LiveRiskError) as raised:
        runtime.control_risk(_request(runtime, **changes))
    assert (raised.value.status, raised.value.code) == (
        400,
        "invalid_live_risk_request",
    )
    assert runtime.risk_snapshot() == original


def test_full_history_retains_receipts_and_replays_without_duplicate_effect(
    runtime, monkeypatch
):
    import g15.live.risk as module

    monkeypatch.setitem(module.LIMITS, "max_history", 1)
    monkeypatch.setitem(module.LIMITS, "max_requests", 2)
    request = _request(runtime)
    runtime.control_risk(request)
    runtime.control_risk(_request(runtime, "clear"))
    with pytest.raises(LiveRiskError) as raised:
        runtime.control_risk(_request(runtime))
    assert raised.value.code == "live_risk_history_full"
    replayed = runtime.control_risk(request)
    assert replayed["request"]["replayed"] is True
    assert replayed["version"] == 2
    assert replayed["devices"][0]["event"]["status"] == "cancelled"
    ledger = json.loads((runtime.session_root / "risk-ledger.json").read_text())
    assert len(ledger["requests"]) == 2
    assert len(ledger["events"]) == 1
