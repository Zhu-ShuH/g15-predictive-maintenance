"""Failure-oriented checks for the opt-in engineering evidence runner.

No producer or RNG is called; configurations use only public regression values.
"""

import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "g15_final_validation_tool", ROOT / "tools/run_final_validation.py"
)
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


@pytest.fixture
def frozen_inputs(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "configs").mkdir(parents=True)
    (source / "tools").mkdir()
    shutil.copyfile(Path(RUNNER.__file__), source / "tools/run_final_validation.py")
    monkeypatch.setattr(
        RUNNER, "__file__", str(source / "tools/run_final_validation.py")
    )
    for name in ("consumer.yaml", "health_rules.yaml"):
        shutil.copyfile(ROOT / "configs" / name, source / "configs" / name)
    cases = []
    for slot, seed in ((1, 42), (2, 43)):
        for profile in RUNNER.PROFILES:
            template = "healthy" if profile == "normal" else profile
            name = f"acceptance_{template}_seed42.yaml"
            shutil.copyfile(ROOT / "configs" / name, source / "configs" / name)
            config = yaml.safe_load(
                (ROOT / "configs" / name).read_text(encoding="utf-8")
            )
            config["scenario"]["seed"] = seed
            config["scenario"]["id"] = f"fixture-{slot}-{profile}"
            if profile in ("normal", "degradation"):
                config["scenario"]["kind"] = profile
            path = tmp_path / f"fixture-{slot}-{profile}.yaml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            cases.append(
                {
                    "slot_id": slot,
                    "seed": seed,
                    "profile": profile,
                    "path": path.name,
                    "sha256": RUNNER.digest(path),
                }
            )
    manifest = {
        "format": "g15-final-validation-v1",
        "status": "frozen",
        "rules_frozen": True,
        "authorization_receipt": "https://example.com/test-fixture-only",
        "frozen_at_utc": "2026-11-01T00:00:00Z",
        "source_commit": "a" * 40,
        "runner_sha256": RUNNER.digest(Path(RUNNER.__file__)),
        "consumer_config": {
            "path": "source/configs/consumer.yaml",
            "sha256": RUNNER.digest(source / "configs/consumer.yaml"),
        },
        "health_rules": {
            "path": "source/configs/health_rules.yaml",
            "sha256": RUNNER.digest(source / "configs/health_rules.yaml"),
        },
        "cases": cases,
    }
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        RUNNER, "_git", lambda source, *args: "a" * 40 if args[0] == "rev-parse" else ""
    )
    return source, path, manifest


def test_preflight_checks_inputs_without_running_any_cli(frozen_inputs, monkeypatch):
    source, path, _ = frozen_inputs
    monkeypatch.setattr(
        RUNNER.subprocess, "run", lambda *a, **kw: pytest.fail("CLI executed")
    )
    assert RUNNER.main(["--source", str(source), "--manifest", str(path)]) == 0
    assert not (path.parent / "runs").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "proposed_unfrozen"),
        ("rules_frozen", False),
        ("authorization_receipt", ""),
        ("authorization_receipt", "not a receipt URL"),
        ("frozen_at_utc", "2026-11-01T00:00:00"),
        ("frozen_at_utc", "2099-01-01T00:00:00Z"),
        ("runner_sha256", "0" * 64),
        ("source_commit", "b" * 40),
    ],
)
def test_preflight_rejects_unfrozen_or_changed_material(frozen_inputs, field, value):
    source, path, manifest = frozen_inputs
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        RUNNER.preflight(path, source)


def test_changed_rules_are_rejected_before_output_is_created(frozen_inputs):
    source, path, _ = frozen_inputs
    (source / "configs/health_rules.yaml").write_text("changed", encoding="utf-8")
    output = path.parent / "output"
    with pytest.raises(ValueError, match="digest mismatch"):
        RUNNER.execute(path, source, output)
    assert not output.exists()


def test_dirty_source_is_rejected(frozen_inputs, monkeypatch):
    source, path, _ = frozen_inputs
    monkeypatch.setattr(
        RUNNER,
        "_git",
        lambda source, *args: (
            "a" * 40 if args[0] == "rev-parse" else " M src/g15/config.py"
        ),
    )
    with pytest.raises(ValueError, match="local changes"):
        RUNNER.preflight(path, source)


def test_rehashed_retuned_profile_is_still_rejected(frozen_inputs):
    source, path, manifest = frozen_inputs
    case = manifest["cases"][2]
    config_path = path.parent / case["path"]
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["fault_generation"]["events"][0]["amplitude"] = 50.0
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    case["sha256"] = RUNNER.digest(config_path)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="reviewed fixed profile"):
        RUNNER.preflight(path, source)


def test_duplicate_case_cannot_replace_missing_matrix_cell(frozen_inputs):
    source, path, manifest = frozen_inputs
    manifest["cases"][-1] = copy.deepcopy(manifest["cases"][0])
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate case"):
        RUNNER.preflight(path, source)


def report(profile):
    fault = profile in ("spike", "step", "burst")
    return {
        "scenario_kind": "fault" if fault else profile,
        "verifiable": True,
        "passed": True,
        "fault_events": int(fault),
        "detected_events": int(fault),
        "missed_events": 0,
        "detection_rate": 1.0 if fault else None,
        "alarm_count": int(fault),
        "false_alarm_count": 0,
        "redundant_alarm_count": 0,
        "unmatched_alarm_count": 0,
        "events": (
            [{"detected": True, "type": profile, "matched_alarm_start": 16384}]
            if fault
            else []
        ),
        "total_points": 65536,
        "processed_points": 65536,
        "analysed_points": 65536,
        "analysis_window_points": 256,
        "feature_windows": 256,
        "rejected_points": 0,
        "dropped_points": 0,
    }


def test_fault_false_alarms_are_reported_without_new_zero_gate():
    payload = report("spike")
    payload.update(
        alarm_count=3,
        false_alarm_count=1,
        redundant_alarm_count=1,
        unmatched_alarm_count=2,
    )
    assert RUNNER.evaluate_report("spike", payload, [])["passed"] is True


def test_normal_alarm_fails_even_if_report_claims_pass():
    payload = report("normal")
    payload.update(alarm_count=1, false_alarm_count=1, unmatched_alarm_count=1)
    assert RUNNER.evaluate_report("normal", payload, [])["passed"] is False


def test_degradation_requires_actual_medians_and_preserves_alarms():
    payload = report("degradation")
    payload.update(alarm_count=1, false_alarm_count=1, unmatched_alarm_count=1)
    assert (
        RUNNER.evaluate_report("degradation", payload, [80.0] * 128 + [60.0] * 128)[
            "passed"
        ]
        is True
    )
    assert (
        RUNNER.evaluate_report("degradation", payload, [80.0] * 256)["passed"] is False
    )
    assert RUNNER.evaluate_report("degradation", payload, [])["passed"] is False


@pytest.mark.parametrize(
    "key", ["fault_events", "false_alarm_count", "events", "analysed_points"]
)
def test_missing_evidence_never_becomes_zero_or_success(key):
    payload = report("spike")
    payload.pop(key)
    assert RUNNER.evaluate_report("spike", payload, [])["passed"] is False


def test_missing_mapping_or_wrong_geometry_cannot_pass():
    payload = report("spike")
    payload["events"][0]["matched_alarm_start"] = None
    payload["analysis_window_points"] = 64
    assert RUNNER.evaluate_report("spike", payload, [])["passed"] is False


@pytest.mark.parametrize("events", [[None], ["invalid"], [1]])
def test_malformed_event_returns_failure_without_losing_batch(events):
    payload = report("spike")
    payload["events"] = events
    assert RUNNER.evaluate_report("spike", payload, [])["passed"] is False


def test_output_cannot_create_untracked_files_in_pinned_source(frozen_inputs):
    source, path, _ = frozen_inputs
    with pytest.raises(ValueError, match="outside the source checkout"):
        RUNNER.execute(path, source, source / "evidence")
    assert not (source / "evidence").exists()


def test_execute_flag_requires_output(frozen_inputs):
    source, path, _ = frozen_inputs
    assert (
        RUNNER.main(["--source", str(source), "--manifest", str(path), "--execute"])
        == 2
    )


@pytest.mark.parametrize("replace_manifest", [False, True])
def test_frozen_material_drift_aborts_before_generation(
    frozen_inputs, monkeypatch, replace_manifest
):
    source, path, manifest = frozen_inputs
    original_bytes = path.read_bytes()
    original_hash = RUNNER.digest(path)
    commands = []

    def fake_cli(command, **kwargs):
        commands.append(command)
        # Replace a config with another syntactically valid identity; the old
        # implementation accepted the replacement if its freeze was rehashed.
        config_path = path.parent / manifest["cases"][0]["path"]
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["scenario"]["id"] = "replaced-during-batch"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        if replace_manifest:
            manifest["cases"][0]["sha256"] = RUNNER.digest(config_path)
            manifest["authorization_receipt"] = "https://example.com/replacement-freeze"
            path.write_text(json.dumps(manifest), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(RUNNER.subprocess, "run", fake_cli)
    monkeypatch.setattr(RUNNER.platform, "platform", lambda: "unit-test-fixture")
    output = path.parent / "batch"
    result = RUNNER.execute(path, source, output)
    assert [command[3] for command in commands] == ["validate"]
    assert result["passed"] is False
    assert result["aborted"] is True
    assert result["unstarted_cases"] == 9
    assert result["manifest_sha256"] == original_hash
    assert (output / "frozen-input.json").read_bytes() == original_bytes
    assert "FrozenInputsChanged" in result["abort_reason"]
    assert json.loads((output / "result.json").read_text(encoding="utf-8")) == result
