"""Short actual-HTTP diagnostics for the opt-in one-minute observation CLI."""

from __future__ import annotations

import base64
import hashlib
import gc
import json
import multiprocessing
import threading
import weakref
from copy import deepcopy

import pytest

from tools.query_load import observation_runner


def _index(output):
    return [
        json.loads(line) for line in (output / "index.ndjson").read_text().splitlines()
    ]


def test_multiple_actual_waves_reuse_server_and_keep_deduplicated_evidence(tmp_path):
    output = tmp_path / "two-waves"
    summary = observation_runner.run_observation(
        output,
        concurrent=10,
        duration_seconds=0.001,
        min_waves=2,
        max_waves=2,
    )
    assert summary["quality_passed"], summary
    assert summary["duration_met"] is True
    assert summary["formal_one_minute_met"] is False
    assert summary["gate0_passed"] is False
    assert summary["completed_waves"] == 2
    assert summary["observed_load_seconds"] >= 0.001
    assert summary["wall_observation_seconds"] >= summary["observed_load_seconds"]
    assert summary["between_wave_gap_seconds"] >= 0
    rows = _index(output)
    assert [row["phase"] for row in rows] == [
        "serial10_and_full_paging",
        "concurrent",
        "concurrent",
    ]
    all_ids = set()
    pids = set()
    target_flags = []
    for row in rows:
        raw_path = output / row["raw"]["path"]
        raw = raw_path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["raw"]["sha256"]
        report_path = output / row["report"]["path"]
        report_bytes = report_path.read_bytes()
        assert hashlib.sha256(report_bytes).hexdigest() == row["report"]["sha256"]
        assert b"response_bytes_base64" not in report_bytes
        report = json.loads(report_bytes)
        assert report["quality_passed"], report
        assert report["format"] == "g15-d0-observation-wave-v3"
        assert not [
            json.loads(line)
            for line in raw.splitlines()
            if json.loads(line)["kind"]
            in ("transport", "application", "network", "marker", "resource")
        ]
        for artifact in row["raw"]["auxiliary"]:
            payload = (output / artifact["path"]).read_bytes()
            assert artifact["bytes"] == len(payload)
            assert artifact["sha256"] == hashlib.sha256(payload).hexdigest()
        assert row["raw"]["total_bytes"] == len(raw) + sum(
            item["bytes"] for item in row["raw"]["auxiliary"]
        )
        assert report["observer_evidence"]["publication_cost"]["wall_seconds"] >= 0
        assert report["observer_evidence"]["publication_cost"]["cpu_seconds"] >= 0
        assert not any(report["checks"].values())
        pids.add(report["server_ready_process_id"])
        records = list(observation_runner.iter_wave_records(output, row["raw"]))
        clients = [item["value"] for item in records if item["kind"] == "client"]
        server = [item["value"] for item in records if item["kind"] == "transport"]
        markers = [
            item["value"]["marker"] for item in records if item["kind"] == "marker"
        ]
        assert markers == ["begin", "end"]
        ids = {client["server_request_id"] for client in clients}
        assert not ids & all_ids
        all_ids |= ids
        assert {event["request_id"] for event in server} == ids
        assert len(server) == len(clients) * 4
        for client in clients:
            body = json.loads(base64.b64decode(client["response_bytes_base64"]))
            assert body["request_id"] == client["server_request_id"]
        if row["phase"] == "concurrent":
            assert len(clients) == 10
            target_flags.append(report["target_reached"])
    assert pids == {summary["server_process_id"]}
    assert summary["format"] == "g15-d0-observation-v3"
    assert (
        summary["stored_evidence_bytes"]
        == sum(row["raw"]["total_bytes"] + row["report"]["bytes"] for row in rows)
        + (output / "index.ndjson").stat().st_size
        + summary["post_shutdown_artifacts"]["bytes"]
    )
    assert summary["post_shutdown_artifacts"]["artifact_delta_bytes"] == 0
    assert not summary["post_shutdown_artifacts"]["problems"]
    assert summary["target_reached_waves"] == sum(target_flags)
    assert summary["all_waves_target_reached"] is all(target_flags)
    assert summary["exit_code"] == int(not all(target_flags))
    assert (
        hashlib.sha256((output / "index.ndjson").read_bytes()).hexdigest()
        == summary["index"]["sha256"]
    )


def test_wave_budget_stops_without_claiming_one_minute_and_preserves_all_waves(
    tmp_path,
):
    output = tmp_path / "bounded"
    summary = observation_runner.run_observation(
        output,
        concurrent=10,
        duration_seconds=60,
        max_waves=1,
    )
    assert summary["exit_code"] == 1
    assert summary["duration_met"] is False
    assert summary["formal_one_minute_met"] is False
    assert summary["stop_reason"] == "wave_budget_exhausted"
    assert summary["completed_waves"] == 1
    assert len(_index(output)) == 2
    assert (output / "wave-00001" / "raw.ndjson").is_file()


def test_wrong_actual_response_stops_first_wave_and_keeps_original_wire_bytes(
    tmp_path, monkeypatch
):
    original = observation_runner._concurrent_wave

    def wrong_response(clients):
        responses = original(clients)
        responses[0][1]["device_id"] = "tampered-after-wire"
        return responses

    monkeypatch.setattr(observation_runner, "_concurrent_wave", wrong_response)
    output = tmp_path / "bad-wave"
    summary = observation_runner.run_observation(
        output,
        concurrent=10,
        duration_seconds=60,
        max_waves=4,
    )
    assert summary["completed_waves"] == 1
    assert summary["exit_code"] == 1
    assert summary["stop_reason"] == "wave_quality_failed"
    rows = _index(output)
    assert len(rows) == 2
    report = json.loads((output / rows[-1]["report"]["path"]).read_bytes())
    assert report["quality_passed"] is False
    raw = (output / rows[-1]["raw"]["path"]).read_bytes()
    assert b"tampered-after-wire" not in raw
    clients = [
        json.loads(line)["value"]
        for line in raw.splitlines()
        if json.loads(line)["kind"] == "client"
    ]
    assert len(clients) == 10
    assert all(client["response_bytes_base64"] for client in clients)


def test_cli_refuses_diagnostic_short_duration(tmp_path):
    output = tmp_path / "never-created"
    with pytest.raises(SystemExit) as error:
        observation_runner.main(
            ["--concurrent", "10", "--seconds", "0.01", "--output", str(output)]
        )
    assert error.value.code == 2
    assert not output.exists()


def test_interruption_after_real_wave_closes_hashes_indexes_and_propagates(
    tmp_path, monkeypatch
):
    original = observation_runner._concurrent_wave
    interruption = KeyboardInterrupt("private interrupt contents")

    def interrupt_after_query(clients):
        original(clients)
        raise interruption

    monkeypatch.setattr(observation_runner, "_concurrent_wave", interrupt_after_query)
    output = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt) as error:
        observation_runner.run_observation(
            output,
            concurrent=10,
            duration_seconds=60,
            max_waves=4,
        )
    assert error.value is interruption
    summary = json.loads((output / "summary.json").read_bytes())
    assert summary["interrupted"] is True
    assert summary["active_wave"] is None
    assert summary["exit_code"] == 1
    assert summary["quality_passed"] is False
    assert summary["gate0_passed"] is False
    rows = _index(output)
    assert len(rows) == 2
    last = rows[-1]
    raw = (output / last["raw"]["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == last["raw"]["sha256"]
    report = json.loads((output / last["report"]["path"]).read_bytes())
    assert report["interrupted"] is True
    assert report["quality_passed"] is False
    clients = [
        json.loads(line)["value"]
        for line in raw.splitlines()
        if json.loads(line)["kind"] == "client"
    ]
    assert len(clients) == 10
    assert all(client["response_bytes_base64"] for client in clients)
    assert b"private interrupt contents" not in (output / "summary.json").read_bytes()


@pytest.mark.parametrize("mode", ["drop", "alter"])
def test_persisted_server_network_must_match_control_snapshot(
    tmp_path, monkeypatch, mode
):
    original = observation_runner.WaveEvidence.stream

    def corrupt_stream(self, message):
        if message.get("kind") == "network":
            if mode == "drop":
                return
            message = dict(message)
            message["event"] = dict(message["event"], bytes_sent_known=-1)
        original(self, message)

    monkeypatch.setattr(observation_runner.WaveEvidence, "stream", corrupt_stream)
    output = tmp_path / mode
    summary = observation_runner.run_observation(
        output,
        concurrent=10,
        duration_seconds=0.001,
        max_waves=1,
    )
    assert summary["quality_passed"] is False
    assert summary["exit_code"] == 1
    rows = _index(output)
    assert len(rows) == 1
    report = json.loads((output / rows[0]["report"]["path"]).read_bytes())
    assert "network stream/snapshot mismatch" in report["checks"]["stream"]


def test_wrong_origin_wave_identity_is_kept_in_raw_evidence(tmp_path):
    state = observation_runner.WaveEvidence(tmp_path, "current")
    state.stream(
        {
            "kind": "marker",
            "wave_id": "wrong-origin",
            "event": {"marker": "end", "wave_id": "wrong-origin"},
        }
    )
    facts = state.close()
    row = json.loads((tmp_path / facts["path"]).read_bytes())
    assert row["wave_id"] == "current"
    assert row["origin_wave_id"] == "wrong-origin"
    assert "cross-wave event identity" in facts["errors"]


def test_hash_covers_actual_bytes_after_failed_flush(tmp_path):
    state = observation_runner.WaveEvidence(tmp_path, "flush-failure")
    original = state.handle

    class FailedFlush:
        def write(self, payload):
            return original.write(payload)

        def flush(self):
            original.flush()
            raise OSError("injected flush failure")

        def fileno(self):
            return original.fileno()

        def close(self):
            return original.close()

    state.handle = FailedFlush()
    with pytest.raises(OSError):
        state.write("diagnostic", {"already_written": True})
    facts = state.close()
    raw = (tmp_path / facts["path"]).read_bytes()
    assert raw
    assert facts["bytes"] == len(raw)
    assert facts["sha256"] == hashlib.sha256(raw).hexdigest()
    assert facts["counts"] == {}
    assert facts["errors"]


def test_late_report_failure_retains_wave_without_second_finish(tmp_path, monkeypatch):
    original = observation_runner._atomic_json
    failed = False

    def fail_once(path, value):
        nonlocal failed
        if (
            path.name == "report.json"
            and path.parent.name == "wave-00001"
            and not failed
        ):
            failed = True
            raise OSError("private report write contents")
        return original(path, value)

    monkeypatch.setattr(observation_runner, "_atomic_json", fail_once)
    output = tmp_path / "late-report-failure"
    summary = observation_runner.run_observation(
        output,
        concurrent=10,
        duration_seconds=60,
        max_waves=4,
    )
    assert failed
    assert summary["quality_passed"] is False
    assert summary["exit_code"] == 1
    rows = _index(output)
    assert len(rows) == 2
    last = rows[-1]
    raw = (output / last["raw"]["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == last["raw"]["sha256"]
    records = list(observation_runner.iter_wave_records(output, last["raw"]))
    assert [row["value"]["marker"] for row in records if row["kind"] == "marker"] == [
        "begin",
        "end",
    ]
    assert len([row for row in records if row["kind"] == "client"]) == 10
    report = json.loads((output / last["report"]["path"]).read_bytes())
    assert report["quality_passed"] is False
    assert {"phase": "observation_unhandled", "error": "OSError"} in report["failures"]


PUBLICATION_MODE = "commit-framed mmap during HTTP; seal and IPC publication after handlers and sampler stop"


def _publication_resources(pid=321):
    before = {
        "process_id": pid,
        "endpoint": "server_observer_publication",
        "clock": "perf_counter",
        "monotonic_ns": 100,
        "cpu_user_seconds": 1.0,
        "cpu_system_seconds": 0.5,
    }
    return {
        "before": before,
        "after": dict(before, monotonic_ns=200, cpu_user_seconds=1.25),
        "scope": "post HTTP seal/fsync and IPC",
    }


def _spooled_state(tmp_path, *, wave_id="wave-unit", messages=None):
    state = observation_runner.WaveEvidence(tmp_path, wave_id, server_spooled=True)
    state.server_process_id = 321
    spool = observation_runner.WaveEventSpool(state.directory, wave_id, max_bytes=4096)
    messages = messages or [
        {
            "kind": "transport",
            "wave_id": wave_id,
            "event": {
                "stage": "response_completed",
                "request_id": "r1",
                "monotonic_ns": 50,
                "process_id": 321,
            },
        }
    ]
    for message in messages:
        spool.append(message["kind"], message["event"])
        state.stream(deepcopy(message))
    facts = spool.seal()
    state.observer_evidence = {
        "mode": PUBLICATION_MODE,
        "spool": facts,
        "published_frames": len(messages),
        "publication_resources": _publication_resources(),
    }
    return state, spool


def test_spool_receipt_is_independent_and_normal_raw_has_no_server_duplicate(tmp_path):
    state, _spool = _spooled_state(tmp_path)
    state.write("client", {"response_bytes_base64": "e30="})
    receipt = state.close()
    assert not receipt["errors"], receipt
    assert receipt is state.close()
    assert [row["kind"] for row in receipt["auxiliary"]] == ["server_spool"]
    assert receipt["total_bytes"] == receipt["bytes"] + receipt["auxiliary"][0]["bytes"]
    assert b"response_completed" not in state.path.read_bytes()
    originals = list(observation_runner.iter_wave_records(tmp_path, receipt))
    assert [row["kind"] for row in originals] == ["client", "transport"]
    server = receipt["server_spool"]
    assert server["recovered_frames"] == server["received_frames"] == 1
    assert server["recovery"]["fsync_independently_verified"] is False
    assert server["publication_cost"]["wall_seconds"] == 0.0000001
    assert server["publication_cost"]["cpu_seconds"] == 0.25


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "corrupt",
        "cross_wave",
        "delivered",
        "duplicate",
        "foreign_spool",
        "count",
        "fsync",
        "seal",
        "hash",
        "bytes",
    ],
)
def test_spool_failure_keeps_received_frames_and_every_actual_auxiliary_byte(
    tmp_path, damage
):
    state, spool = _spooled_state(tmp_path)
    if damage == "missing":
        spool.path.unlink()
    elif damage == "corrupt":
        payload = spool.path.read_bytes()
        spool.path.write_bytes(
            payload.replace(b"response_completed", b"response_incorrect")
        )
    elif damage == "foreign_spool":
        other = observation_runner.WaveEventSpool(tmp_path, "foreign", max_bytes=4096)
        other.append("marker", {"marker": "begin"})
        other.seal()
        spool.path.write_bytes(other.path.read_bytes())
    elif damage == "cross_wave":
        state.messages[0]["wave_id"] = "other-wave"
    elif damage == "delivered":
        state.messages[0]["event"]["request_id"] = "tampered"
    elif damage == "duplicate":
        state.messages.append(deepcopy(state.messages[0]))
    elif damage == "count":
        state.observer_evidence["published_frames"] = True
    else:
        field = {
            "fsync": "fsync_succeeded",
            "seal": "sealed",
            "hash": "sha256",
            "bytes": "bytes",
        }[damage]
        state.observer_evidence["spool"][field] = {
            "fsync": False,
            "seal": False,
            "hash": "0" * 64,
            "bytes": 0,
        }[damage]
    receipt = state.close()
    assert receipt["errors"]
    assert receipt["server_spool"]["checks"]
    mismatch = next(
        row for row in receipt["auxiliary"] if row["kind"] == "server_delivery_mismatch"
    )
    assert [
        json.loads(line)
        for line in (tmp_path / mismatch["path"]).read_bytes().splitlines()
    ] == state.messages
    assert receipt["total_bytes"] == receipt["bytes"] + sum(
        row["bytes"] for row in receipt["auxiliary"]
    )
    for artifact in receipt["auxiliary"]:
        payload = (tmp_path / artifact["path"]).read_bytes()
        assert artifact["sha256"] == hashlib.sha256(payload).hexdigest()
        assert artifact["bytes"] == len(payload)


@pytest.mark.parametrize(
    "damage",
    [
        "pid",
        "clock",
        "time",
        "cpu_negative",
        "cpu_regressed",
        "cpu_bool",
        "http_not_finished",
    ],
)
def test_post_http_publication_resource_identity_clock_and_cpu_are_required(
    tmp_path, damage
):
    state, _spool = _spooled_state(tmp_path)
    publication = state.observer_evidence["publication_resources"]
    if damage == "pid":
        publication["after"]["process_id"] = 322
    elif damage == "clock":
        publication["before"]["clock"] = "other-clock"
    elif damage == "time":
        publication["after"]["monotonic_ns"] = 99
    elif damage == "cpu_negative":
        publication["before"]["cpu_system_seconds"] = -1
    elif damage == "cpu_regressed":
        publication["after"]["cpu_user_seconds"] = 0.9
    elif damage == "cpu_bool":
        publication["after"]["cpu_user_seconds"] = True
    else:
        publication["before"]["monotonic_ns"] = 49
    receipt = state.close()
    assert receipt["server_spool"]["checks"]
    assert receipt["errors"]
    assert any(
        row["kind"] == "server_delivery_mismatch" for row in receipt["auxiliary"]
    )


def _killed_spool_writer(directory, wave_id, sealed, sender):
    spool = observation_runner.WaveEventSpool(directory, wave_id, max_bytes=4096)
    spool.append(
        "transport", {"stage": "response_completed", "request_id": "before-kill"}
    )
    if sealed:
        spool.seal()
    sender.send({"ready": True})
    sender.recv()  # Test owns the process termination; no timing sleep.


@pytest.mark.parametrize("sealed", [False, True])
def test_child_kill_recovers_prefix_without_finished_or_fsync_invention(
    tmp_path, sealed
):
    state = observation_runner.WaveEvidence(tmp_path, "killed", server_spooled=True)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe()
    process = context.Process(
        target=_killed_spool_writer,
        args=(str(state.directory), state.wave_id, sealed, sender),
    )
    process.start()
    sender.close()
    try:
        assert receiver.poll(10), "child did not commit its real frame"
        assert receiver.recv() == {"ready": True}
        process.terminate()
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode != 0
        receipt = state.close()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        receiver.close()
    recovery = receipt["server_spool"]["recovery"]
    assert recovery["recovered_messages"] == 1
    assert recovery["seal_marker_present"] is sealed
    assert recovery["fsync_independently_verified"] is False
    assert recovery["incomplete"] is (not sealed)
    assert receipt["errors"]  # Child died without finished/publication; never pass.
    assert receipt["server_spool"]["control"] is None
    originals = list(observation_runner.iter_wave_records(tmp_path, receipt))
    assert originals[0]["value"]["request_id"] == "before-kill"


def test_emergency_and_journal_failure_artifacts_are_not_hidden_from_budget(tmp_path):
    state = observation_runner.WaveEvidence(tmp_path, "overflow", server_spooled=True)
    spool = observation_runner.WaveEventSpool(
        state.directory, state.wave_id, max_bytes=512
    )
    spool.append("marker", {"marker": "begin"})
    with pytest.raises(RuntimeError):
        spool.append("resource", {"padding": "x" * 2048})
    with pytest.raises(RuntimeError):
        spool.close()
    fallback = state.directory / "completion-journal-failure.json"
    fallback.write_bytes(b'{"known_client": true}\n')
    receipt = state.close()
    assert receipt["errors"]
    assert receipt["server_spool"]["recovery"]["emergency_messages"] == 1
    assert {row["kind"] for row in receipt["auxiliary"]} == {
        "server_spool",
        "server_emergency",
        "server_delivery_mismatch",
        "client_journal_failure",
    }
    assert receipt["total_bytes"] > 512 + fallback.stat().st_size
    assert receipt["total_bytes"] == sum(
        path.stat().st_size for path in state.directory.iterdir()
    )


def test_full_unsealed_mapping_and_client_raw_exceed_original_wave_budget(tmp_path):
    state = observation_runner.WaveEvidence(
        tmp_path, "unsealed-budget", server_spooled=True
    )
    spool = observation_runner.WaveEventSpool(state.directory, state.wave_id)
    spool.append("marker", {"marker": "begin"})
    spool.close()
    state.write("client_attempt", {"http_started": False})
    receipt = state.close()
    assert receipt["auxiliary"][0]["bytes"] == observation_runner.MAX_WAVE_BYTES
    assert receipt["total_bytes"] > observation_runner.MAX_WAVE_BYTES
    assert receipt["bytes"] < observation_runner.MAX_WAVE_BYTES


def test_all_actual_http_terminals_precede_blocked_server_evidence_consumer(
    tmp_path, monkeypatch
):
    """Blocking only observer delivery cannot hold any real HTTP completion."""
    from concurrent.futures import ThreadPoolExecutor

    all_terminal, reader_entered, release_reader = (threading.Event() for _ in range(3))
    original_complete = observation_runner.RunAccounting.client_complete
    original_stream = observation_runner.WaveEvidence.stream
    first_delivery_after_terminals = []

    def completed(accounting, record):
        original_complete(accounting, record)
        if len(accounting.client_records) == 10:
            all_terminal.set()

    def blocked_consumer(state, message):
        if not reader_entered.is_set():
            first_delivery_after_terminals.append(all_terminal.is_set())
            reader_entered.set()
            if not release_reader.wait(10):
                raise TimeoutError("test evidence consumer release missing")
        return original_stream(state, message)

    monkeypatch.setattr(observation_runner.RunAccounting, "client_complete", completed)
    monkeypatch.setattr(observation_runner.WaveEvidence, "stream", blocked_consumer)
    runs_root, output = tmp_path / "runs", tmp_path / "evidence"
    output.mkdir()
    db_paths = observation_runner.build_d0_runs(runs_root)
    bearer, cursor = b"b" * 32, b"c" * 32
    identities = observation_runner._identities(bearer, 10)
    session = observation_runner.ObservationSession()
    session.start(runs_root, bearer, cursor, 10, 64, evidence_root=output)
    try:
        with ThreadPoolExecutor(max_workers=1) as observer:
            result = observer.submit(
                session.observe, output, "blocked-consumer", identities, db_paths
            )
            try:
                assert reader_entered.wait(15), "no server evidence delivery"
                assert (
                    all_terminal.is_set()
                ), "HTTP completion depended on evidence consumer"
                assert first_delivery_after_terminals == [True]
            finally:
                release_reader.set()
            report, receipt = result.result(timeout=30)
        assert report["quality_passed"], report
        assert report["completed_requests"] == 10
        assert not report["checks"]["network"]
        records = list(observation_runner.iter_wave_records(output, receipt["raw"]))
        clients = [row["value"] for row in records if row["kind"] == "client"]
        server = [row["value"] for row in records if row["kind"] == "transport"]
        assert len(clients) == 10
        assert all(row["status"] == 200 for row in clients)
        assert {row["server_request_id"] for row in clients} == {
            row["request_id"] for row in server
        }
        before = report["observer_evidence"]["control"]["publication_resources"][
            "before"
        ]
        assert before["process_id"] == session.ready["process_id"]
        assert before["monotonic_ns"] >= max(
            row["monotonic_ns"]
            for row in server
            if row["stage"] == "response_completed"
        )
    finally:
        release_reader.set()
        errors = session.close()
    assert not errors


def _index_closed_state(output, state, receipt):
    report_path = state.directory / "report.json"
    report_path.write_bytes(b"{}\n")
    row = {
        "wave_id": state.wave_id,
        "raw": receipt,
        "report": observation_runner._file_receipt(report_path, output, "report"),
    }
    index = output / "index.ndjson"
    index.write_bytes(observation_runner._json_bytes(row))
    return receipt["total_bytes"] + report_path.stat().st_size + index.stat().st_size


def test_post_shutdown_manifest_detects_late_emergency_after_cached_receipt(tmp_path):
    state, spool = _spooled_state(tmp_path)
    receipt = state.close()
    indexed = _index_closed_state(tmp_path, state, receipt)
    original_index = (tmp_path / "index.ndjson").read_bytes()
    with pytest.raises(RuntimeError):
        spool.append(
            "transport", {"stage": "response_completed", "request_id": "late-known"}
        )
    with pytest.raises(RuntimeError):
        spool.close()
    # Per-wave receipt stays the original claim; the independent final manifest
    # must find the bytes generated afterwards without rewriting that claim.
    assert state.close() is receipt
    result = observation_runner._post_shutdown_artifact_manifest(tmp_path, indexed)
    assert result["problems"]
    assert result["affected_waves"] == [state.wave_id]
    emergency = spool.emergency_path.read_bytes()
    assert b"late-known" in emergency
    assert result["artifact_delta_bytes"] == len(emergency)
    assert result["actual_stored_bytes"] == indexed + len(emergency) + result["bytes"]
    assert (tmp_path / "index.ndjson").read_bytes() == original_index
    manifest = json.loads((tmp_path / result["path"]).read_bytes())
    late = [row for row in manifest["artifacts"] if row["status"] == "new"]
    assert len(late) == 1
    assert late[0]["actual"]["sha256"] == hashlib.sha256(emergency).hexdigest()


def test_post_shutdown_manifest_receipts_root_unattributed_crash_prefix(tmp_path):
    (tmp_path / "index.ndjson").write_bytes(b"")
    spool = observation_runner.WaveEventSpool(tmp_path, "unattributed", max_bytes=4096)
    spool.append(
        "unattributed",
        {
            "origin_wave_id": None,
            "original_kind": "transport",
            "original_event": {"stage": "server_received"},
        },
    )
    spool.close()
    result = observation_runner._post_shutdown_artifact_manifest(tmp_path, 0)
    assert result["problems"]
    assert result["affected_waves"] == ["unattributed"]
    assert result["artifact_delta_bytes"] == 4096
    manifest = json.loads((tmp_path / result["path"]).read_bytes())
    assert manifest["artifacts"][0]["recovery"]["recovered_messages"] == 1
    assert manifest["artifacts"][0]["recovery"]["incomplete"] is True
    assert manifest["artifacts"][0]["recovery"]["fsync_independently_verified"] is False


@pytest.mark.parametrize("damage", ["change", "delete"])
def test_post_shutdown_manifest_keeps_original_receipt_when_file_changes(
    tmp_path, damage
):
    state, spool = _spooled_state(tmp_path)
    receipt = state.close()
    indexed = _index_closed_state(tmp_path, state, receipt)
    before = spool.path.read_bytes()
    if damage == "change":
        spool.path.write_bytes(before + b"late")
    else:
        spool.path.unlink()
    result = observation_runner._post_shutdown_artifact_manifest(tmp_path, indexed)
    assert result["problems"]
    assert result["affected_waves"] == [state.wave_id]
    assert result["artifact_delta_bytes"] == (4 if damage == "change" else -len(before))
    assert receipt["auxiliary"][0]["sha256"] == hashlib.sha256(before).hexdigest()


def _closed_client_sampler_references(tmp_path):
    state = observation_runner.WaveEvidence(tmp_path, "sampler-lifetime")
    sampler = observation_runner.EndpointSampler(
        "client",
        interval_seconds=3600,
        on_sample=lambda row: state.write("resource_client", row),
    )
    state.client_sampler = sampler
    sampler.start()
    resources = sampler.stop()
    assert (
        sampler.stop() is resources
    )  # Stop cannot invent an unpersisted extra final sample.
    receipt = state.close()
    return weakref.ref(state), weakref.ref(sampler), resources, receipt


def test_closed_wave_sampler_cycle_releases_by_reference_count_and_keeps_final_sample(
    tmp_path,
):
    """Disable cyclic GC only here; stopped waves must not need it to retire."""
    enabled = gc.isenabled()
    gc.disable()
    try:
        state_ref, sampler_ref, resources, receipt = _closed_client_sampler_references(
            tmp_path
        )
        assert (
            state_ref() is None
        ), "closed wave still retained through sampler callback"
        assert (
            sampler_ref() is None
        ), "stopped sampler still belongs to a reference cycle"
        raw = (tmp_path / receipt["path"]).read_bytes()
        rows = [json.loads(line) for line in raw.splitlines()]
        assert not resources["errors"]
        assert len(resources["samples"]) == 2  # Initial and stop's final capture.
        assert [row["value"] for row in rows] == resources["samples"]
        assert receipt["counts"] == {"resource_client": 2}
        assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    finally:
        if enabled:
            gc.enable()


def test_sampler_that_cannot_join_keeps_late_callback_and_original_failure():
    """An unproven worker stop must not discard known future observations."""
    captured = []
    sampler = observation_runner.EndpointSampler("client", on_sample=captured.append)

    class StillRunning:
        def join(self, timeout):
            assert timeout == 5

        def is_alive(self):
            return True

    sampler._thread = StillRunning()
    resources = sampler.stop()
    assert resources["errors"] == ["sampler_thread_not_stopped"]
    assert sampler._on_sample is not None
    assert captured == resources["samples"]
    assert len(captured) == 1
    # Represent the already-running worker's eventual capture: it is preserved.
    sampler._capture()
    assert len(captured) == 2
    assert captured == resources["samples"]


def test_interrupted_final_capture_keeps_callback_and_propagates_without_fake_stop(
    monkeypatch,
):
    sampler = observation_runner.EndpointSampler("client", on_sample=lambda _row: None)
    callback = sampler._on_sample
    interrupted = KeyboardInterrupt("unit-only interruption")

    def interrupt():
        raise interrupted

    monkeypatch.setattr(sampler, "_capture", interrupt)
    with pytest.raises(KeyboardInterrupt) as error:
        sampler.stop()
    assert error.value is interrupted
    assert sampler._on_sample is callback
    assert sampler._stop_result is None


def test_interrupted_stop_report_construction_retains_callback_for_real_cleanup(
    monkeypatch,
):
    from tools.query_load import process_server

    captured = []
    sampler = observation_runner.EndpointSampler("client", on_sample=captured.append)
    callback = sampler._on_sample
    host_snapshot = process_server.host_snapshot
    interrupted = KeyboardInterrupt("unit-only report interruption")

    def interrupt_host():
        raise interrupted

    monkeypatch.setattr(process_server, "host_snapshot", interrupt_host)
    with pytest.raises(KeyboardInterrupt) as error:
        sampler.stop()
    assert error.value is interrupted
    assert len(captured) == 1
    assert sampler._on_sample is callback
    assert sampler._stop_result is None
    monkeypatch.setattr(process_server, "host_snapshot", host_snapshot)
    resources = sampler.stop()
    assert captured == resources["samples"]
    assert len(captured) == 2
    assert sampler._on_sample is None
    assert sampler.stop() is resources
    assert len(captured) == 2


def _completed_prelude_references(tmp_path):
    state = observation_runner.WaveEvidence(tmp_path, "prelude-lifetime")
    accounting = observation_runner.PersistedAccounting(state)
    state.accounting = accounting
    started = accounting.client_start()
    accounting.client_complete(
        observation_runner.ClientRecord(
            "actor",
            "run",
            "device",
            started,
            started,
            started,
            200,
            "request",
            response_bytes_base64="e30=",
        )
    )
    count = len(accounting.client_records)
    receipt = state.close()
    state.release_runtime_references()
    return weakref.ref(state), weakref.ref(accounting), count, receipt


def test_closed_prelude_accounting_cycle_releases_without_cyclic_gc_and_keeps_raw(
    tmp_path,
):
    enabled = gc.isenabled()
    gc.disable()
    try:
        state_ref, accounting_ref, count, receipt = _completed_prelude_references(
            tmp_path
        )
        assert state_ref() is None, "closed prelude retained by PersistedAccounting"
        assert accounting_ref() is None
        assert count == 1
        raw = (tmp_path / receipt["path"]).read_bytes()
        record = json.loads(raw)["value"]
        assert record["server_request_id"] == "request"
        assert record["response_bytes_base64"] == "e30="
        assert receipt["counts"] == {"client": 1}
        assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    finally:
        if enabled:
            gc.enable()


def test_runtime_release_requires_closed_evidence_and_keeps_live_sampler(tmp_path):
    state = observation_runner.WaveEvidence(tmp_path, "still-live")
    sampler = observation_runner.EndpointSampler(
        "client", on_sample=lambda row: state.write("resource_client", row)
    )
    state.client_sampler = sampler
    callback = sampler._on_sample
    with pytest.raises(RuntimeError):
        state.release_runtime_references()
    assert state.client_sampler is sampler
    state.close()
    state.release_runtime_references()
    assert (
        state.client_sampler is sampler
    )  # stop has not proved this producer finished.
    assert sampler._on_sample is callback


def test_interrupted_observation_releases_accounting_only_after_preserving_completed_count(
    tmp_path, monkeypatch
):
    session = observation_runner.ObservationSession()
    state = observation_runner.WaveEvidence(tmp_path, "interrupted-lifetime")
    accounting = observation_runner.PersistedAccounting(state)
    state.accounting = accounting
    for index in range(3):
        started = accounting.client_start()
        accounting.client_complete(
            observation_runner.ClientRecord(
                "actor",
                "run",
                "device",
                started,
                started,
                started,
                200,
                f"request-{index}",
                response_bytes_base64="e30=",
            )
        )
    state.end.set()  # All original delivery is already present; no live server in this pure unit.
    interruption = KeyboardInterrupt("unit-only observer interruption")

    def fail_observation(*_args, **_kwargs):
        session.current = state
        raise interruption

    monkeypatch.setattr(session, "_observe", fail_observation)
    monkeypatch.setattr(session, "close", lambda: [])
    with pytest.raises(KeyboardInterrupt) as error:
        session.observe(tmp_path, state.wave_id, [], [], prelude=True)
    assert error.value is interruption
    report, facts = interruption.observation_wave_result
    assert report["completed_requests"] == 3
    assert report["quality_passed"] is False
    assert report["interrupted"] is True
    assert state.accounting is None
    assert session.current is None
    raw = (tmp_path / facts["raw"]["path"]).read_bytes()
    assert len([json.loads(line) for line in raw.splitlines()]) == 3
    assert facts["raw"]["sha256"] == hashlib.sha256(raw).hexdigest()
