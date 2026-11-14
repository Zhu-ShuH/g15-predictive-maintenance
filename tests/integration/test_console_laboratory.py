"""Independent bounded laboratory runs use actual producer/consumer/reporting."""

from copy import deepcopy
import inspect
import json
from pathlib import Path
import sqlite3
import stat
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from g15.console import laboratory as lab

RULES = Path(__file__).resolve().parents[2] / "configs" / "health_rules.yaml"


def _wait(controller, identity, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = controller.detail(identity)
        if result["experiment"]["status"] in {"completed", "failed"}:
            return result
        time.sleep(0.01)
    pytest.fail("laboratory experiment did not finish")


def _create(controller, parameters):
    for _ in range(100):
        try:
            return controller.create(deepcopy(parameters))["experiment"]["id"]
        except lab.LaboratoryRequestError as exc:
            if exc.code != "laboratory_busy":
                raise
            time.sleep(0.005)
    pytest.fail("laboratory worker did not release completed run")


@pytest.fixture
def laboratory(tmp_path):
    controller = lab.LaboratoryController(tmp_path / "laboratory", RULES)
    try:
        yield controller
    finally:
        controller.close()


@pytest.mark.parametrize("preset", lab.PRESETS)
def test_profiles_persist_config_and_return_real_analysis_and_report(
    laboratory, preset
):
    identity = _create(laboratory, preset)
    detail = _wait(laboratory, identity)
    experiment, results = detail["experiment"], detail["results"]
    assert experiment["status"] == "completed", experiment
    assert experiment["parameters"] == preset
    assert experiment["elapsed_seconds"] > 0
    run_dir, device = laboratory.run_directory(identity)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    report = json.loads((run_dir / "reports" / "summary.json").read_text())
    assert manifest["status"] == "complete" and manifest["schema_version"] == "2.0"
    assert manifest["seed"] == preset["seed"]
    assert report["device_id"] == device
    assert results["summary"] == {key: report.get(key) for key in lab._SUMMARY_FIELDS}
    assert len(results["windows"]) == preset["total_points"] // 256
    assert results["summary"]["processed_points"] == preset["total_points"]
    with sqlite3.connect(run_dir / "artifacts" / "results.sqlite") as conn:
        windows = conn.execute(
            "SELECT start_seq,end_seq_exclusive,health_score FROM feature_windows ORDER BY start_seq"
        ).fetchall()
    assert windows == [
        (w["start_seq"], w["end_seq_exclusive"], w["health_score"])
        for w in results["windows"]
    ]
    config_path = laboratory.root / identity / "scenario.yaml"
    assert config_path.is_file()
    assert (
        laboratory.root / identity / "health-rules.yaml"
    ).read_bytes() == RULES.read_bytes()
    assert (
        json.loads((laboratory.root / identity / "parameters.json").read_text())
        == preset
    )
    public = json.dumps(detail)
    assert str(laboratory.root) not in public and "private_truth" not in public
    if preset["profile"] == "sudden_fault":
        assert detail["configuration"]["fault_start_seq"] == preset["total_points"] // 2
        assert results["summary"]["fault_events"] == 1
    else:
        assert detail["configuration"]["fault_start_seq"] is None
        assert results["summary"]["detection_rate"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"seed": True},
        {"seed": -1},
        {"seed": 2**31},
        {"total_points": 8193},
        {"total_points": 65537},
        {"total_points": 4096},
        {"noise_std": -0.1},
        {"noise_std": float("nan")},
        {"noise_std": 1.1},
        {"degradation_rate_per_sample": 0.1},
        {"degradation_rate_per_sample": 0.0001},
        {"fault_amplitude": 1},
        {"profile": "../../private"},
        {"path": "private"},
    ],
)
def test_invalid_parameters_are_rejected_before_any_experiment(laboratory, changes):
    with pytest.raises(lab.LaboratoryRequestError) as error:
        laboratory.create({**lab.PRESETS[0], **changes})
    assert (error.value.status, error.value.code) == (400, "invalid_request")
    assert laboratory.overview()["experiments"] == []
    assert list(laboratory.root.glob("lab-*")) == []


def test_maximum_size_remains_bounded_and_complete(laboratory):
    identity = _create(laboratory, {**lab.PRESETS[1], "total_points": 65536})
    detail = _wait(laboratory, identity)
    assert detail["experiment"]["status"] == "completed"
    assert len(detail["results"]["windows"]) == 256
    assert len(list((laboratory.root / identity).rglob("*.npz"))) == 16
    assert laboratory._disk_bytes() < lab._RESERVE_BYTES


def test_repeat_seed_preserves_raw_arrays_and_scores_in_independent_runs(laboratory):
    first = _create(laboratory, lab.PRESETS[2])
    first_result = _wait(laboratory, first)
    second = _create(laboratory, lab.PRESETS[2])
    second_result = _wait(laboratory, second)
    assert first != second
    first_run, first_device = laboratory.run_directory(first)
    second_run, second_device = laboratory.run_directory(second)
    assert first_device != second_device and first_run != second_run
    for left, right in zip(
        sorted(first_run.glob("raw/*.npz")), sorted(second_run.glob("raw/*.npz"))
    ):
        with (
            np.load(left, allow_pickle=False) as a,
            np.load(right, allow_pickle=False) as b,
        ):
            for key in ("seq", "timestamp_ns", "vibration", "temperature"):
                assert np.array_equal(a[key], b[key])
    assert first_result["results"]["windows"] == second_result["results"]["windows"]


def test_busy_requests_are_not_queued_and_incomplete_waveform_is_rejected(
    laboratory, monkeypatch
):
    started, release = threading.Event(), threading.Event()
    produce = lab.produce_run_detailed

    def blocked(*args):
        started.set()
        assert release.wait(5)
        return produce(*args)

    monkeypatch.setattr(lab, "produce_run_detailed", blocked)
    identity = _create(laboratory, lab.PRESETS[0])
    try:
        assert started.wait(5)
        with pytest.raises(lab.LaboratoryRequestError) as busy:
            laboratory.create(lab.PRESETS[1])
        assert (busy.value.status, busy.value.code) == (409, "laboratory_busy")
        with pytest.raises(lab.LaboratoryRequestError) as pending:
            laboratory.run_directory(identity)
        assert pending.value.code == "experiment_not_ready"
        assert len(laboratory.overview()["experiments"]) == 1
    finally:
        release.set()
    assert _wait(laboratory, identity)["experiment"]["status"] == "completed"


def test_only_reporting_reads_truth_and_consumer_receives_no_fault_config(
    laboratory, monkeypatch
):
    open_path = Path.open
    reads = []

    def checked_open(path, mode="r", *args, **kwargs):
        if "private_truth" in path.parts and "r" in mode:
            assert any(
                Path(frame.filename).name in {"builder.py", "metrics.py"}
                for frame in inspect.stack()
            )
            reads.append(path.name)
        return open_path(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    identity = _create(laboratory, lab.PRESETS[2])
    assert _wait(laboratory, identity)["experiment"]["status"] == "completed"
    assert reads


def test_history_survives_restart_and_interrupted_job_is_never_success(tmp_path):
    root = tmp_path / "laboratory"
    controller = lab.LaboratoryController(root, RULES)
    identity = _create(controller, lab.PRESETS[0])
    original = _wait(controller, identity)
    controller.close()
    with lab_context(root) as restored:
        assert restored.detail(identity) == original
    record_path = root / identity / "experiment.json"
    record = json.loads(record_path.read_text())
    record["status"] = "analysing"
    record_path.write_text(json.dumps(record))
    with lab_context(root) as restored:
        detail = restored.detail(identity)
        assert detail["experiment"]["status"] == "failed"
        assert detail["experiment"]["error"] == "interrupted"
        assert detail["results"] is None
        assert (root / identity / "results.json").is_file()


class lab_context:
    """Close controllers even if a persistence assertion fails."""

    def __init__(self, root):
        self.controller = lab.LaboratoryController(root, RULES)

    def __enter__(self):
        return self.controller

    def __exit__(self, *_args):
        self.controller.close()


def test_failures_are_recorded_without_private_exception_text(laboratory, monkeypatch):
    def failed(*_args):
        raise OSError("private-path-and-secret")

    monkeypatch.setattr(lab, "produce_run_detailed", failed)
    identity = _create(laboratory, lab.PRESETS[0])
    detail = _wait(laboratory, identity)
    assert detail["experiment"]["status"] == "failed"
    assert detail["experiment"]["error"] == "experiment_failed"
    assert "private-path-and-secret" not in json.dumps(detail)
    assert (laboratory.root / identity / "scenario.yaml").is_file()


def test_history_and_storage_limits_never_delete_existing_evidence(
    laboratory, monkeypatch
):
    identity = _create(laboratory, lab.PRESETS[0])
    _wait(laboratory, identity)
    original = (laboratory.root / identity / "experiment.json").read_bytes()
    monkeypatch.setitem(lab.LIMITS, "max_experiments", 1)
    with pytest.raises(lab.LaboratoryRequestError) as full:
        _create(laboratory, lab.PRESETS[0])
    assert full.value.status == 429
    monkeypatch.setitem(lab.LIMITS, "max_experiments", 20)
    monkeypatch.setattr(laboratory, "_disk_bytes", lambda: lab.LIMITS["max_disk_bytes"])
    with pytest.raises(lab.LaboratoryRequestError) as storage:
        _create(laboratory, lab.PRESETS[0])
    assert storage.value.status == 507
    assert (laboratory.root / identity / "experiment.json").read_bytes() == original


def test_unknown_identity_closed_service_and_single_writer_are_rejected(laboratory):
    with pytest.raises(RuntimeError):
        lab.LaboratoryController(laboratory.root, RULES)
    with pytest.raises(lab.LaboratoryRequestError) as unknown:
        laboratory.detail("../../secret")
    assert unknown.value.status == 404
    laboratory.close()
    with pytest.raises(lab.LaboratoryRequestError) as closed:
        laboratory.create(lab.PRESETS[0])
    assert closed.value.status == 503


@pytest.mark.parametrize("change", ["run", "score", "summary", "extra", "geometry"])
def test_restored_results_validate_identity_values_and_public_fields(
    laboratory, change
):
    identity = _create(laboratory, lab.PRESETS[0])
    _wait(laboratory, identity)
    result_path = laboratory.root / identity / "results.json"
    result = json.loads(result_path.read_text())
    if change == "run":
        result["run_id"] = "another-run"
    elif change == "score":
        result["windows"][0]["health_score"] = float("nan")
    elif change == "summary":
        result["summary"]["health_score_min"] = 0
    elif change == "extra":
        result["private_path"] = "must-not-be-public"
    else:
        result["windows"][0]["end_seq_exclusive"] = 512
    result_path.write_text(json.dumps(result))
    with pytest.raises(lab.LaboratoryRequestError) as invalid:
        laboratory.detail(identity)
    assert invalid.value.status == 503
    laboratory.close()
    with pytest.raises(ValueError):
        lab.LaboratoryController(laboratory.root, RULES)


def test_windows_junction_attribute_is_rejected_without_a_symlink_mode(
    tmp_path, monkeypatch
):
    root = tmp_path / "junction"
    root.mkdir()
    original = Path.lstat

    def junction(path, *args, **kwargs):
        if path == root:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", junction)
    with pytest.raises(ValueError, match="cannot be a symlink"):
        lab.LaboratoryController(root, RULES)
