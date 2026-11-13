"""Short actual-HTTP diagnostics for the opt-in one-minute observation CLI."""

from __future__ import annotations

import base64
import hashlib
import json

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
        assert not any(report["checks"].values())
        pids.add(report["server_ready_process_id"])
        records = [json.loads(line) for line in raw.splitlines()]
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
    records = [json.loads(line) for line in raw.splitlines()]
    assert [row["value"]["marker"] for row in records if row["kind"] == "marker"] == [
        "begin",
        "end",
    ]
    assert len([row for row in records if row["kind"] == "client"]) == 10
    report = json.loads((output / last["report"]["path"]).read_bytes())
    assert report["quality_passed"] is False
    assert {"phase": "observation_unhandled", "error": "OSError"} in report["failures"]
