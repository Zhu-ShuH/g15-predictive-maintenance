"""Real persisted analysis replay and independent, durable operator actions."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from g15.console.laboratory import LaboratoryController, PRESETS
from g15.console.operations import OperationsController, OperationsRequestError
from g15.live import LiveConfig, LiveRuntime

RULES = Path(__file__).resolve().parents[2] / "configs" / "health_rules.yaml"


def _wait(getter, ready, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = getter()
        if ready(value):
            return value
        time.sleep(0.01)
    pytest.fail("real source did not become ready")


@pytest.fixture
def experiment(tmp_path):
    laboratory = LaboratoryController(tmp_path / "laboratory", RULES)
    identity = laboratory.create(deepcopy(PRESETS[2]))["experiment"]["id"]
    detail = _wait(
        lambda: laboratory.detail(identity),
        lambda value: value["experiment"]["status"] in {"completed", "failed"},
    )
    assert detail["experiment"]["status"] == "completed"
    runs = tmp_path / "current-runs"
    runs.mkdir()
    controller = OperationsController(
        tmp_path / "operations",
        runs_root=runs,
        catalog_provider=lambda: [],
        authorize=lambda _pair: None,
        laboratory=laboratory,
    )
    try:
        yield controller, laboratory, "experiment:" + identity
    finally:
        controller.close()
        laboratory.close()


def _request(controller, identity, **changes):
    detail = controller.detail(identity)
    assert detail["alarms"]
    return {
        "id": identity,
        "alarm_key": detail["alarms"][0]["alarm_key"],
        "action": "acknowledge",
        "operator": "本机验收员",
        "note": "已查看真实波形，待复测。",
        "request_id": "request-00000001",
        **changes,
    }


def test_replay_is_real_complete_analysis_and_no_truth_read(experiment, monkeypatch):
    controller, laboratory, identity = experiment
    run_dir, device = laboratory.run_directory(identity.split(":", 1)[1])
    original = Path.open

    def public_only(path, *args, **kwargs):
        assert "private_truth" not in path.parts
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", public_only)
    detail = controller.detail(identity)
    with sqlite3.connect(run_dir / "artifacts" / "results.sqlite") as conn:
        windows = conn.execute(
            "SELECT start_seq,end_seq_exclusive,health_score FROM feature_windows ORDER BY start_seq"
        ).fetchall()
        count = conn.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]
    assert windows == [
        (row["start_seq"], row["end_seq_exclusive"], row["health_score"])
        for row in detail["windows"]
    ]
    assert detail["summary"]["alarm_count"] == count
    assert detail["summary"]["processed_points"] == 8192
    assert detail["available_points"] == 8192
    assert detail["run"]["device_id"] == device
    assert detail["complete"] is True
    assert detail["latency"]["kind"] == "experiment_elapsed"
    assert detail["latency"]["count"] is None
    assert detail["latency"]["mean_seconds"] is None
    assert detail["latency"]["elapsed_seconds"] > 0
    assert detail["configuration"]["parameters"] == PRESETS[2]
    assert detail["configuration"]["rule_ids"]
    assert all(row["advice"] for row in detail["alarms"])
    by_start = {row[0]: row[1] for row in windows}
    for alarm in detail["alarms"]:
        assert alarm["end_seq_exclusive"] == by_start[alarm["start_seq"]]
        assert alarm["available_seq"] == by_start[alarm["start_seq"]] - 1
        assert alarm["available_seq"] > alarm["start_seq"]
    assert str(run_dir) not in json.dumps(detail)
    assert controller.resolve_waveform(identity) == (
        run_dir.parent,
        run_dir.name,
        device,
    )


def test_comparison_preserves_units_configuration_and_unknowns(experiment):
    controller, _laboratory, identity = experiment
    result = controller.compare([identity])
    assert result["alignment"] == "elapsed_seconds"
    assert len(result["runs"]) == 1
    detail = result["runs"][0]
    assert detail["available_points"] == 8192
    assert detail["windows"][1]["time_seconds"] == 1
    assert detail["latency"]["mean_seconds"] is None
    assert "alarms" not in detail and "dispositions" not in detail
    for bad in ([], [identity, identity], [identity] * 5, "not-a-list", [None]):
        with pytest.raises(OperationsRequestError) as error:
            controller.compare(bad)
        assert error.value.status == 400


def test_operator_trail_is_append_only_idempotent_and_persists(experiment):
    controller, laboratory, identity = experiment
    payload = _request(controller, identity)
    run_dir, _device = laboratory.run_directory(identity.split(":", 1)[1])
    database = run_dir / "artifacts" / "results.sqlite"
    before = database.read_bytes()
    first = controller.record_disposition(payload)
    duplicate = controller.record_disposition(payload)
    second = controller.record_disposition(
        {
            **payload,
            "request_id": "request-00000002",
            "action": "note",
            "note": "追加复测记录 <script>literal text</script>",
        }
    )
    assert first["duplicate"] is False and duplicate["duplicate"] is True
    assert duplicate["event"] == first["event"]
    assert second["event"]["previous_hash"] == first["event"]["event_hash"]
    assert database.read_bytes() == before
    assert len(controller.dispositions(identity)["events"]) == 2
    with sqlite3.connect(controller.root / "dispositions.sqlite") as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM disposition_events")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE disposition_events SET source_id='changed'")
    controller.close()
    restored = OperationsController(
        controller.root,
        runs_root=controller.runs_root,
        catalog_provider=lambda: [],
        authorize=lambda _pair: None,
        laboratory=laboratory,
    )
    try:
        assert restored.dispositions(identity)["events"] == [
            first["event"],
            second["event"],
        ]
    finally:
        restored.close()


def test_disposition_cannot_invent_alarm_or_change_existing_request(experiment):
    controller, _laboratory, identity = experiment
    payload = _request(controller, identity)
    controller.record_disposition(payload)
    for changes, code in (
        ({"note": "changed"}, "request_conflict"),
        ({"request_id": "request-00000002"}, "already_acknowledged"),
        ({"alarm_key": "0" * 64}, "not_found"),
    ):
        with pytest.raises(OperationsRequestError) as error:
            controller.record_disposition({**payload, **changes})
        assert error.value.code == code
    assert len(controller.dispositions(identity)["events"]) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"operator": ""},
        {"operator": "a" * 81},
        {"note": " "},
        {"note": "a" * 2001},
        {"note": "nul\x00"},
        {"action": "reset"},
        {"alarm_key": "../"},
        {"request_id": "bad"},
        {"extra": "unknown"},
    ],
)
def test_disposition_input_boundaries(experiment, changes):
    controller, _laboratory, identity = experiment
    with pytest.raises(OperationsRequestError) as error:
        controller.record_disposition(_request(controller, identity, **changes))
    assert error.value.status == 400
    assert controller.dispositions(identity)["events"] == []


def test_parallel_duplicate_requests_append_once(experiment):
    controller, _laboratory, identity = experiment
    payload = _request(controller, identity)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(controller.record_disposition, [payload] * 4))
    assert sum(not result["duplicate"] for result in results) == 1
    assert len(controller.dispositions(identity)["events"]) == 1


def test_close_during_slow_detail_cannot_append_after_releasing_writer(
    experiment, monkeypatch
):
    controller, _laboratory, identity = experiment
    payload = _request(controller, identity)
    original = controller.detail
    detail_ready = threading.Event()
    continue_request = threading.Event()

    def delayed(source):
        result = original(source)
        detail_ready.set()
        assert continue_request.wait(5)
        return result

    monkeypatch.setattr(controller, "detail", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controller.record_disposition, payload)
        assert detail_ready.wait(5)
        controller.close()
        continue_request.set()
        with pytest.raises(OperationsRequestError) as error:
            future.result(timeout=5)
    assert error.value.status == 503
    assert error.value.code == "operations_unavailable"
    with sqlite3.connect(controller.root / "dispositions.sqlite") as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM disposition_events").fetchone()[0] == 0
        )


def test_available_points_retains_raw_high_water_when_windows_have_gaps(experiment):
    controller, laboratory, identity = experiment
    directory, _device = laboratory.run_directory(identity.split(":", 1)[1])
    with sqlite3.connect(directory / "artifacts" / "results.sqlite") as conn:
        conn.execute("DELETE FROM feature_windows WHERE start_seq=0")
    detail = controller.detail(identity)
    assert detail["available_points"] == 8192
    assert detail["summary"]["processed_points"] == 8192 - 256
    assert detail["windows"][0]["start_seq"] == 256


def test_unknown_sources_and_altered_manifest_are_rejected(experiment):
    controller, laboratory, identity = experiment
    with pytest.raises(OperationsRequestError) as error:
        controller.detail("live:../../outside")
    assert error.value.status == 404
    run_dir, _device = laboratory.run_directory(identity.split(":", 1)[1])
    path = run_dir / "manifest.json"
    value = json.loads(path.read_text())
    value["device_id"] = "OTHER"
    path.write_text(json.dumps(value))
    with pytest.raises(OperationsRequestError) as error:
        controller.detail(identity)
    assert error.value.code == "invalid_source"


def test_limit_fails_explicitly_without_partial_result(experiment, monkeypatch):
    from g15.console import operations

    controller, _laboratory, identity = experiment
    monkeypatch.setitem(operations.LIMITS, "max_windows", 1)
    with pytest.raises(OperationsRequestError) as error:
        controller.detail(identity)
    assert error.value.status == 413 and error.value.code == "source_too_large"


def test_ledger_damage_is_detected_on_restart(experiment):
    controller, laboratory, identity = experiment
    controller.record_disposition(_request(controller, identity))
    controller.close()
    with sqlite3.connect(controller.root / "dispositions.sqlite") as conn:
        conn.execute("DROP TRIGGER dispositions_no_update")
        conn.execute(
            "UPDATE disposition_events SET event_json=REPLACE(event_json, 'acknowledge', 'tampered')"
        )
    with pytest.raises(ValueError, match="integrity"):
        OperationsController(
            controller.root,
            runs_root=controller.runs_root,
            catalog_provider=lambda: [],
            authorize=lambda _pair: None,
            laboratory=laboratory,
        )


def test_response_budget_and_ledger_limit_fail_without_truncation(
    experiment, monkeypatch
):
    from g15.console import operations

    controller, _laboratory, identity = experiment
    payload = _request(controller, identity)
    controller.record_disposition(payload)
    monkeypatch.setitem(operations.LIMITS, "max_dispositions", 1)
    with pytest.raises(OperationsRequestError) as error:
        controller.record_disposition(
            {**payload, "action": "note", "request_id": "request-00000002"}
        )
    assert error.value.status == 429
    monkeypatch.setattr(operations, "_MAX_RESPONSE_BYTES", 100)
    for operation in (controller.dispositions, controller.detail):
        with pytest.raises(OperationsRequestError) as error:
            operation(identity)
        assert error.value.status == 413


def test_reparse_analysis_ancestor_is_rejected(experiment, monkeypatch):
    from types import SimpleNamespace

    controller, laboratory, identity = experiment
    directory, _device = laboratory.run_directory(identity.split(":", 1)[1])
    original = Path.lstat

    def changed(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == directory / "artifacts":
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", changed)
    with pytest.raises(OperationsRequestError) as error:
        controller.resolve_waveform(identity)
    assert error.value.code == "invalid_source"


def test_current_authorization_and_archived_identity_and_latency(tmp_path):
    runtime = LiveRuntime(
        tmp_path / "live",
        health_rules_path=RULES,
        config=LiveConfig(
            devices=2,
            sample_rate_hz=160,
            window_points=16,
            interval_seconds=0.1,
            max_duration_seconds=5,
            cycle_windows=4,
        ),
    )
    runtime.start()
    _wait(
        runtime.status_snapshot,
        lambda status: all(row["processed_samples"] >= 64 for row in status["devices"]),
    )
    runtime.stop()
    denied = [False]
    calls = []

    def authorize(pair):
        calls.append(pair)
        if denied[0]:
            raise OperationsRequestError(403, "forbidden")

    controller = OperationsController(
        tmp_path / "operations",
        runs_root=runtime.runs_root,
        catalog_provider=runtime.catalog,
        authorize=authorize,
        live_status_provider=runtime.status_snapshot,
    )
    try:
        identities = [row["id"] for row in controller.overview()["runs"]]
        comparison = controller.compare(identities)
        assert len(comparison["runs"]) == 2 and len(calls) == 2
        for result in comparison["runs"]:
            assert result["latency"]["kind"] == "capture_to_commit"
            assert result["latency"]["count"] > 0
            assert result["latency"]["mean_seconds"] >= 0
            assert result["configuration"]["sample_rate_hz"] == 160
            assert result["configuration"]["parameters"]["seed"] == runtime.config.seed
            assert result["run"]["updated_at_utc"].endswith("Z")
        denied[0] = True
        for operation in (
            controller.detail,
            controller.dispositions,
            controller.resolve_waveform,
        ):
            with pytest.raises(OperationsRequestError) as error:
                operation(identities[0])
            assert error.value.status == 403
    finally:
        controller.close()
    status = runtime.status_snapshot()
    history = [
        {
            **row,
            "source_kind": "archive",
            "session_id": runtime.session_id,
            "run_dir": runtime.runs_root / row["run_id"],
            "status": "stopped",
        }
        for row in status["devices"]
    ]
    archived = OperationsController(
        tmp_path / "archive-operations",
        runs_root=runtime.runs_root,
        catalog_provider=lambda: [],
        authorize=lambda _pair: pytest.fail(
            "archived grant does not use current token"
        ),
        history_provider=lambda: history,
    )
    try:
        result = archived.detail(identities[0])
        assert result["run"]["source_kind"] == "archive"
        assert result["latency"]["kind"] == "capture_to_commit"
        history[0]["session_id"] = "changed-session"
        with pytest.raises(OperationsRequestError) as error:
            archived.detail(identities[0])
        assert error.value.code == "invalid_source"
    finally:
        archived.close()
