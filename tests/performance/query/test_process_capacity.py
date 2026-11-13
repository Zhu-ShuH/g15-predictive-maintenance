"""Real subprocess integration checks for endpoint isolation and evidence."""

from __future__ import annotations

import base64
from copy import deepcopy
import json
import os

import pytest

from tools.query_load import process_runner
from tools.query_load.client import QueryClient
from tools.query_load.process_runner import (
    _validate_resources,
    run_process_measurement,
)
from tools.query_load.process_server import EndpointSampler


@pytest.fixture(scope="module")
def process_report(tmp_path_factory):
    report = run_process_measurement(
        tmp_path_factory.mktemp("real-process-http") / "runs", concurrent=10, serial=1
    )
    assert report["quality_passed"], report.get("checks") or report.get("failures")
    return report


def test_two_process_http_keeps_raw_records_and_origin_stages(process_report):
    """An actual authenticated HTTP/SQLite run uses a different server PID."""
    report = process_report
    resources = report["endpoint_resources"]
    assert resources["client"]["process_id"] == os.getpid()
    assert resources["server"]["process_id"] != os.getpid()
    assert resources["client"]["samples"]
    assert resources["server"]["samples"]
    assert resources["client"]["cpu_total_delta_seconds"] >= 0
    assert resources["server"]["cpu_total_delta_seconds"] >= 0
    assert report["workload"]["page_size"] == 100
    assert len(report["connection_preparation"]) == 10
    for row in report["connection_preparation"]:
        assert row["request_sent"]
        assert row["connect_started_monotonic"] <= row["connected_monotonic"]
        assert row["connected_monotonic"] <= row["barrier_released_monotonic"]
        assert (
            row["barrier_released_monotonic"] <= row["request_send_started_monotonic"]
        )
    assert len({row["user_id"] for row in report["connection_preparation"]}) == 10
    records = report["accounting"]["client_records"]
    responses = (
        report["serial_responses"]
        + report["concurrent_responses"]
        + report["paging"]["raw_responses"]
    )
    originals = {
        row["server_request_id"]: json.loads(
            base64.b64decode(row["response_bytes_base64"])
        )
        for row in records
    }
    for status, body, _run in responses:
        assert status == 200
        assert originals[body["request_id"]] == body
    pid = resources["server"]["process_id"]
    assert report["accounting"]["server_clock_process_ids"] == [pid]
    assert all(row["process_id"] == pid for row in report["transport_events"])
    assert all(type(row["monotonic_ns"]) is int for row in report["transport_events"])
    assert report["accounting"]["application_queue"] == "absent"
    assert report["accounting"]["server_queued"] is None
    assert report["gate0_passed"] is False
    assert report["latency_by_phase"]["serial"]["count"] == 1
    assert report["latency_by_phase"]["concurrent"]["count"] == 10
    assert report["latency_by_phase"]["paging"]["count"] == len(
        report["paging"]["raw_responses"]
    )
    assert report["latency_by_phase"]["unclassified"]["count"] == 0
    wave_ids = {body["request_id"] for _, body, _ in report["concurrent_responses"]}
    assert {
        row["server_request_id"]
        for row in report["accounting_by_phase"]["concurrent"]["client_records"]
    } == wave_ids
    assert {
        row["request_id"]
        for row in report["accounting_by_phase"]["concurrent"]["server_records"]
    } == wave_ids
    assert len(report["accounting_by_phase"]["concurrent"]["client_records"]) == 10
    assert "includes TCP connect" in report["latency_by_phase"]["serial"]["basis"]
    assert "after TCP preconnect" in report["latency_by_phase"]["concurrent"]["basis"]
    assert report["streamed_evidence"]["resource"] == resources["server"]["samples"]
    assert _validate_resources(report) == []


def test_endpoint_resource_validation_rejects_shared_or_incomplete_process():
    """A label change alone cannot manufacture independently measured resources."""
    sample = {
        "process_id": 7,
        "samples": [{}, {}],
        "errors": [],
        "elapsed_seconds": 1,
        "cpu_total_delta_seconds": 0,
    }
    report = {"endpoint_resources": {"client": sample, "server": dict(sample)}}
    assert "client and server share a process" in _validate_resources(report)
    report["endpoint_resources"]["server"] = {"process_id": 8}
    assert "server lacks start/end resource observations" in _validate_resources(report)


@pytest.mark.parametrize(
    "path,value",
    [
        (("endpoint_resources", "server", "process_id"), -1),
        (("endpoint_resources", "server", "samples", 0, "process_id"), -1),
        (("endpoint_resources", "server", "samples", 0, "endpoint"), "client"),
        (("endpoint_resources", "server", "samples", 0, "clock"), "receipt_clock"),
        (("endpoint_resources", "server", "elapsed_seconds"), 999),
        (("endpoint_resources", "server", "cpu_total_delta_seconds"), 999),
        (("endpoint_resources", "server", "sampled_maxima", "rss_bytes"), 999),
        (
            ("endpoint_resources", "server", "samples", 0, "monotonic_seconds"),
            float("nan"),
        ),
        (("endpoint_resources", "server", "samples", 0, "cpu_user_seconds"), -1),
        (("endpoint_resources", "server", "samples", 0, "rss_bytes"), -1),
        (("server_ready_process_id",), -1),
        (("transport_events", 0, "process_id"), -1),
    ],
)
def test_resource_evidence_tampering_cannot_change_endpoint_or_summary(
    process_report, path, value
):
    report = deepcopy(process_report)
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert _validate_resources(report)


def test_resource_sampler_reports_scope_and_sampled_bounds():
    sampler = EndpointSampler("test")
    sampler.start()
    result = sampler.stop()
    assert result["process_id"] == os.getpid()
    assert result["elapsed_seconds"] > 0
    assert len(result["samples"]) >= 2
    assert "sampled maxima are lower bounds" in result["limitations"][0]


def test_failure_after_real_response_retains_raw_child_and_client_evidence(
    tmp_path, monkeypatch
):
    original = QueryClient.query

    def interrupted(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("private exception contents must not enter evidence")

    monkeypatch.setattr(QueryClient, "query", interrupted)
    report = run_process_measurement(tmp_path / "runs", concurrent=10, serial=1)
    assert report["quality_passed"] is False
    assert report["exit_code"] == 1
    assert report["failures"][0] == {
        "phase": "two_process_run",
        "error": "RuntimeError",
    }
    assert len(report["accounting"]["client_records"]) == 1
    assert len(report["streamed_evidence"]["transport"]) == 4
    assert len(report["streamed_evidence"]["application"]) == 6
    assert report["accounting"]["client_records"][0]["response_bytes_base64"]
    assert report["endpoint_resources"]["client"]["samples"]
    assert report["endpoint_resources"]["server"]["samples"]
    assert "private exception contents" not in json.dumps(report)


def test_cli_late_diagnostics_failure_keeps_completed_raw_report(tmp_path, monkeypatch):
    def fail_late(_records):
        raise ArithmeticError("private diagnostic exception contents")

    monkeypatch.setattr(process_runner, "_latencies", fail_late)
    output = tmp_path / "late-diagnostics-evidence"
    result = process_runner.main(
        [
            "--serial",
            "1",
            "--concurrent",
            "10",
            "--workers",
            "64",
            "--output",
            str(output),
        ]
    )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert result == report["exit_code"] == 1
    assert report["quality_passed"] is False
    assert report["target_reached"] is False
    assert report["gate0_passed"] is False
    assert report["phase_diagnostics_incomplete"] is True
    assert {"phase": "phase_diagnostics", "error": "ArithmeticError"} in report[
        "failures"
    ]
    assert len(report["serial_responses"]) == 1
    assert len(report["concurrent_responses"]) == 10
    assert report["paging"]["raw_responses"]
    assert report["streamed_evidence"]["transport"]
    assert report["streamed_evidence"]["application"]
    assert report["streamed_evidence"]["resource"]
    assert report["endpoint_resources"]["client"]["samples"]
    assert report["endpoint_resources"]["server"]["samples"]
    records = report["accounting"]["client_records"]
    responses = (
        report["serial_responses"]
        + report["concurrent_responses"]
        + report["paging"]["raw_responses"]
    )
    assert len(records) == len(responses)
    originals = {
        record["server_request_id"]: json.loads(
            base64.b64decode(record["response_bytes_base64"])
        )
        for record in records
    }
    assert all(originals[body["request_id"]] == body for _, body, _ in responses)
    assert "private diagnostic exception contents" not in json.dumps(report)


@pytest.mark.parametrize("workers", [0, 9, 65])
def test_process_load_rejects_worker_configuration_before_running(tmp_path, workers):
    with pytest.raises(ValueError):
        run_process_measurement(tmp_path / "unused", concurrent=10, workers=workers)
