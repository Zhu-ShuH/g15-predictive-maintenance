"""Explicit laboratory injections use the existing producer and analysis pipeline."""

from contextlib import contextmanager
from copy import deepcopy
import inspect
import json
from pathlib import Path
import time

import numpy as np
import pytest

from g15.config import load_config
from g15.console import laboratory as lab
from g15.simulation.engine import simulate_chunk
from g15.simulation.faults import FAULT_TYPES, inject_faults

RULES = Path(__file__).resolve().parents[2] / "configs" / "health_rules.yaml"
ARRAYS = ("seq", "timestamp_ns", "vibration", "temperature")


@contextmanager
def _controller(root):
    controller = lab.LaboratoryController(root, RULES)
    try:
        yield controller
    finally:
        controller.close()


@pytest.fixture
def laboratory(tmp_path):
    with _controller(tmp_path / "laboratory") as controller:
        yield controller


def _run(controller, parameters):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            identity = controller.create(deepcopy(parameters))["experiment"]["id"]
            break
        except lab.LaboratoryRequestError as exc:
            if exc.code != "laboratory_busy":
                raise
            time.sleep(0.005)
    else:
        pytest.fail("previous laboratory worker did not finish")
    while time.monotonic() < deadline:
        detail = controller.detail(identity)
        if detail["experiment"]["status"] in {"completed", "failed"}:
            assert detail["experiment"]["status"] == "completed", detail["experiment"]
            return identity, detail
        time.sleep(0.01)
    pytest.fail("injection experiment did not finish")


def _plan(fault_type="burst", **changes):
    return {
        **lab.PRESETS[2],
        "fault_type": fault_type,
        "fault_start_seq": 4001,
        "fault_duration_points": 700,
        **changes,
    }


def _raw(run_dir):
    parts = {key: [] for key in ARRAYS}
    for path in sorted(run_dir.glob("raw/*.npz")):
        with np.load(path, allow_pickle=False) as chunk:
            for key in ARRAYS:
                parts[key].append(chunk[key])
    return {key: np.concatenate(values) for key, values in parts.items()}


@pytest.mark.parametrize("fault_type", FAULT_TYPES)
def test_custom_injection_crosses_chunk_boundary_and_matches_existing_kernel(
    laboratory, fault_type
):
    parameters = _plan(fault_type)
    identity, detail = _run(laboratory, parameters)
    run_dir, _device = laboratory.run_directory(identity)
    config = load_config(laboratory.root / identity / "scenario.yaml")
    raw = _raw(run_dir)
    baseline = simulate_chunk(config, 0, parameters["total_points"])
    # Use the existing public injector on one whole interval. This checks that
    # the producer uses the requested plan across its separate 4096-point files.
    expected, _events = inject_faults(baseline, config)
    for key in ARRAYS:
        np.testing.assert_array_equal(raw[key], getattr(expected, key))
    outside = (raw["seq"] < 4001) | (raw["seq"] >= 4701)
    np.testing.assert_array_equal(
        raw["vibration"][outside], baseline.vibration[outside]
    )
    assert np.any(raw["vibration"][~outside] != baseline.vibration[~outside])
    assert detail["experiment"]["parameters"] == parameters
    assert detail["configuration"]["fault_type"] == fault_type
    assert detail["configuration"]["fault_start_seq"] == 4001
    assert detail["configuration"]["fault_duration_points"] == 700
    assert detail["configuration"]["fault_end_seq_exclusive"] == 4701
    assert detail["configuration"]["fault_amplitude"] == 6.0
    report = json.loads((run_dir / "reports" / "summary.json").read_text())
    event = report["events"][0]
    assert (event["type"], event["start_seq"], event["end_seq_exclusive"]) == (
        fault_type,
        4001,
        4701,
    )
    assert detail["results"]["summary"] == {
        key: report.get(key) for key in lab._SUMMARY_FIELDS
    }
    assert len(detail["results"]["windows"]) == parameters["total_points"] // 256
    assert (
        detail["results"]["summary"]["processed_points"] == parameters["total_points"]
    )


@pytest.mark.parametrize(
    "fault_type,duration", [("spike", 1), ("step", 1), ("burst", 3)]
)
def test_minimum_duration_at_final_sample_is_accepted(laboratory, fault_type, duration):
    parameters = _plan(
        fault_type, fault_start_seq=8192 - duration, fault_duration_points=duration
    )
    _identity, detail = _run(laboratory, parameters)
    assert detail["configuration"]["fault_end_seq_exclusive"] == 8192
    assert detail["results"]["summary"]["fault_events"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"fault_type": "unknown"},
        {"fault_type": []},
        {"fault_start_seq": -1},
        {"fault_start_seq": True},
        {"fault_start_seq": 1.5},
        {"fault_start_seq": 8192},
        {"fault_duration_points": 0},
        {"fault_duration_points": -1},
        {"fault_duration_points": True},
        {"fault_duration_points": 2},
        {"fault_duration_points": 700.0},
        {"fault_duration_points": 8192},
        {"fault_amplitude": float("inf")},
        {"fault_amplitude": True},
        {"fault_amplitude": 10.01},
        {"profile": "normal", "fault_amplitude": 0},
        {"profile": "degradation", "fault_amplitude": 0},
        {"unrecognized_parameter": 1},
    ],
)
def test_invalid_injection_is_rejected_without_job_or_disk_artifacts(
    laboratory, changes
):
    with pytest.raises(lab.LaboratoryRequestError) as error:
        laboratory.create(_plan(**changes))
    assert (error.value.status, error.value.code) == (400, "invalid_request")
    assert laboratory.overview()["experiments"] == []
    assert list(laboratory.root.glob("lab-*")) == []


@pytest.mark.parametrize(
    "missing", ["fault_type", "fault_start_seq", "fault_duration_points"]
)
def test_partial_extension_is_not_silently_defaulted(laboratory, missing):
    parameters = _plan()
    del parameters[missing]
    with pytest.raises(lab.LaboratoryRequestError) as error:
        laboratory.create(parameters)
    assert error.value.status == 400
    assert laboratory.overview()["experiments"] == []


def test_zero_strength_preserves_baseline_and_does_not_fabricate_detection(laboratory):
    identity, detail = _run(laboratory, _plan(noise_std=0.0, fault_amplitude=0.0))
    run_dir, _device = laboratory.run_directory(identity)
    np.testing.assert_array_equal(_raw(run_dir)["vibration"], np.full(8192, 0.5))
    assert detail["results"]["summary"]["fault_events"] == 1
    assert detail["results"]["summary"]["detected_events"] == 0
    assert detail["results"]["summary"]["missed_events"] == 1
    assert detail["results"]["alarms"] == []


def test_explicit_seed_and_plan_reproduce_raw_data_and_analysis(laboratory):
    parameters = _plan(seed=183, fault_amplitude=3.25, noise_std=0.13)
    first_id, first = _run(laboratory, parameters)
    second_id, second = _run(laboratory, parameters)
    first_raw = _raw(laboratory.run_directory(first_id)[0])
    second_raw = _raw(laboratory.run_directory(second_id)[0])
    for key in ARRAYS:
        np.testing.assert_array_equal(first_raw[key], second_raw[key])
    assert first["results"]["windows"] == second["results"]["windows"]
    assert [
        (alarm["start_seq"], alarm["rule_id"], alarm["level"])
        for alarm in first["results"]["alarms"]
    ] == [
        (alarm["start_seq"], alarm["rule_id"], alarm["level"])
        for alarm in second["results"]["alarms"]
    ]


@pytest.mark.parametrize("explicit", [False, True])
def test_legacy_and_explicit_completed_history_survive_restart_without_rewriting(
    tmp_path, explicit
):
    root = tmp_path / "laboratory"
    parameters = _plan("step") if explicit else dict(lab.PRESETS[2])
    with _controller(root) as controller:
        identity, detail = _run(controller, parameters)
    snapshots = {
        name: (root / identity / name).read_bytes()
        for name in (
            "experiment.json",
            "parameters.json",
            "scenario.yaml",
            "results.json",
        )
    }
    with _controller(root) as restored:
        assert restored.detail(identity) == detail
        assert restored.detail(identity)["experiment"]["parameters"] == parameters
    for name, original in snapshots.items():
        assert (root / identity / name).read_bytes() == original
    if not explicit:
        assert len(parameters) == 6
        assert detail["configuration"]["fault_type"] == "spike"
        assert detail["configuration"]["fault_start_seq"] == 4096
        assert detail["configuration"]["fault_duration_points"] == 512


def test_injected_analysis_keeps_truth_reads_in_reporting(laboratory, monkeypatch):
    original_open = Path.open
    reads = []

    def guarded_open(path, mode="r", *args, **kwargs):
        if "private_truth" in path.parts and "r" in mode:
            assert any(
                Path(frame.filename).name in {"builder.py", "metrics.py"}
                for frame in inspect.stack()
            )
            reads.append(path.name)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    _run(laboratory, _plan())
    assert reads


def test_public_fault_capabilities_are_detached_from_internal_limits(laboratory):
    response = laboratory.overview()
    assert response["limits"]["fault_types"] == list(FAULT_TYPES)
    assert response["limits"]["fault_min_duration_points"] == {
        "spike": 1,
        "step": 1,
        "burst": 3,
    }
    response["limits"]["fault_types"].append("unsupported")
    response["limits"]["fault_min_duration_points"]["burst"] = 1
    fresh = laboratory.overview()
    assert fresh["limits"]["fault_types"] == list(FAULT_TYPES)
    assert fresh["limits"]["fault_min_duration_points"]["burst"] == 3
    assert all(len(preset) == 6 for preset in fresh["presets"])
