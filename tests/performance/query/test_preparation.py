"""Pre-HTTP readiness is separate from real request/user concurrency."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from tools.query_load import observation_runner
from tools.query_load.preparation import (
    ReaderReadiness,
    connection_key,
    verify_preparation,
)


def reference(port=40001):
    return {
        "local": {"host": "127.0.0.1", "port": port},
        "peer": {"host": "127.0.0.1", "port": 18000},
    }


def reverse(value):
    return {"local": value["peer"], "peer": value["local"]}


def test_preparation_requires_exact_active_connections_and_never_reuses_old_wave():
    registry = ReaderReadiness()
    client = reference()
    with pytest.raises(TimeoutError):
        registry.wait([client], timeout=0)
    registry.installed(reverse(client))
    with pytest.raises(ValueError):
        registry.wait([client, client], timeout=0)
    assert len(registry.wait([client], timeout=0)["readers"]) == 1
    with pytest.raises(RuntimeError):
        registry.clear()
    registry.closed(reverse(client))
    with pytest.raises(RuntimeError):
        registry.wait([client], timeout=0)
    registry.clear()
    with pytest.raises(TimeoutError):
        registry.wait([client], timeout=0)


@pytest.mark.parametrize(
    "damage",
    [
        "wave",
        "pid",
        "pid_float",
        "started",
        "bool_started",
        "endpoint",
        "time",
        "closed",
        "duplicate",
    ],
)
def test_control_ack_cannot_substitute_a_different_wave_socket_or_started_request(
    damage,
):
    clients = [reference(), reference(40002)]
    ack = {
        "wave_id": "wave-1",
        "process_id": 321,
        "http_started_count": 0,
        "confirmed_monotonic_ns": 20,
        "readers": [
            {"connection_ref": reverse(row), "reader_installed_ns": 10, "active": True}
            for row in clients
        ],
    }
    assert not verify_preparation(ack, clients, wave_id="wave-1", process_id=321)
    bad = deepcopy(ack)
    if damage == "wave":
        bad["wave_id"] = "old-wave"
    elif damage == "pid":
        bad["process_id"] = 123
    elif damage == "pid_float":
        bad["process_id"] = 321.0
    elif damage == "started":
        bad["http_started_count"] = 1
    elif damage == "bool_started":
        bad["http_started_count"] = False
    elif damage == "endpoint":
        bad["readers"][0]["connection_ref"]["peer"]["port"] += 1
    elif damage == "time":
        bad["readers"][0]["reader_installed_ns"] = 21
    elif damage == "closed":
        bad["readers"][0]["active"] = False
    elif damage == "duplicate":
        bad["readers"][1] = bad["readers"][0]
    assert verify_preparation(bad, clients, wave_id="wave-1", process_id=321)


@pytest.mark.parametrize("concurrent", [10, 50])
def test_actual_http_readiness_precedes_send_without_counting_prepared_as_received(
    tmp_path, concurrent
):
    output = tmp_path / f"prepared-{concurrent}"
    summary = observation_runner.run_observation(
        output, concurrent=concurrent, duration_seconds=0.001, min_waves=1, max_waves=1
    )
    assert summary["quality_passed"], summary
    assert not summary["formal_one_minute_met"]
    report = json.loads((output / "wave-00001" / "report.json").read_bytes())
    prep = report["connection_preparation"]
    assert not prep["problems"]
    assert prep["server"]["http_started_count"] == 0
    assert len(prep["server"]["readers"]) == concurrent
    raw = [
        json.loads(line)
        for line in (output / "wave-00001" / "raw.ndjson").read_bytes().splitlines()
    ]
    mappings = [row["value"] for row in raw if row["kind"] == "request_connection"]
    received = [
        row["value"]
        for row in raw
        if row["kind"] == "transport" and row["value"]["stage"] == "server_received"
    ]
    assert len(mappings) == len(received) == concurrent
    installed = {
        connection_key(row["connection_ref"]): row["reader_installed_ns"]
        for row in prep["server"]["readers"]
    }
    for row in received:
        assert (
            installed[connection_key(row["connection_ref"])]
            <= prep["server"]["confirmed_monotonic_ns"]
            <= row["monotonic_ns"]
        )
    for row in mappings:
        timing = row["preparation"]
        assert (
            timing["connected_ns"]
            <= timing["headers_prepared_ns"]
            <= timing["launch_released_ns"]
            <= timing["send_started_ns"]
            <= timing["send_completed_ns"]
            <= timing["body_complete_ns"]
            <= timing["decoded_ns"]
        )
        assert prep["client_confirmed_ns"] <= timing["launch_released_ns"]
        assert timing["launch_mode"] == "selector"
        assert timing["http_started"] is True
        assert "barrier_released_ns" not in timing
        assert row["latency_eligible"] is True
        assert timing["request_sent"] is True
        assert "windows_cursor" not in row["request_context"]
        assert "alarms_cursor" not in row["request_context"]


def test_failed_preparation_retains_its_record_and_sends_no_http(tmp_path, monkeypatch):
    receive = observation_runner._receive

    def fail_confirmation(control, expected):
        value = receive(control, expected)
        if expected == "prepared":
            raise TimeoutError("injected control timeout after actual reader readiness")
        return value

    monkeypatch.setattr(observation_runner, "_receive", fail_confirmation)
    output = tmp_path / "preparation-failed"
    summary = observation_runner.run_observation(
        output, concurrent=10, duration_seconds=0.001, min_waves=1, max_waves=1
    )
    assert not summary["quality_passed"]
    assert summary["exit_code"] == 1
    report = json.loads((output / "wave-00001" / "report.json").read_bytes())
    record = report["connection_preparation"]
    assert record["error_type"] == "TimeoutError"
    assert record["client_confirmed_ns"] is None
    assert record["client_wait_completed_ns"] >= record["client_wait_started_ns"]
    raw = [
        json.loads(line)
        for line in (output / "wave-00001" / "raw.ndjson").read_bytes().splitlines()
    ]
    assert [row["value"] for row in raw if row["kind"] == "preparation"] == [record]
    requests = [row["value"] for row in raw if row["kind"] == "request_connection"]
    assert len(requests) == 10
    assert all(row["preparation"]["request_sent"] is False for row in requests)
    assert all(row["preparation"]["http_started"] is False for row in requests)
    assert all(row["preparation"]["send_started_ns"] is None for row in requests)
    assert not [row for row in raw if row["kind"] == "client"]
    assert len([row for row in raw if row["kind"] == "client_attempt"]) == 10
    assert report["completed_requests"] == 0
    assert report["accounting"]["latency_seconds"]["count"] == 0
    assert report["load_interval"]["seconds"] == 0
    assert report["load_interval"]["counted_toward_duration"] is False
    assert not [row for row in raw if row["kind"] == "transport"]
    index = [
        json.loads(line) for line in (output / "index.ndjson").read_text().splitlines()
    ]
    import hashlib

    assert (
        hashlib.sha256((output / "wave-00001" / "raw.ndjson").read_bytes()).hexdigest()
        == index[-1]["raw"]["sha256"]
    )
