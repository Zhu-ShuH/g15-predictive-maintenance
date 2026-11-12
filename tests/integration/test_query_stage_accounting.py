"""Execution intervals use their original server clock and fail closed."""

from copy import deepcopy

import pytest

from tools.query_load.accounting import ClientRecord, RunAccounting


def _events(request_id="request-1", *, process_id=321):
    return [
        {
            "request_id": request_id,
            "trace_id": request_id,
            "stage": stage,
            "result": result,
            "monotonic_ns": instant * 1_000_000_000,
            "process_id": process_id,
        }
        for instant, stage, result in (
            (10, "server_received", "received"),
            (11, "server_executing", "started"),
            (14, "server_execution_completed", "success"),
            (15, "response_completed", "success"),
        )
    ]


def _accounting(events):
    accounting = RunAccounting()
    for request_id, user in (("request-1", "actor-1"),):
        started = accounting.client_start()
        accounting.client_complete(
            ClientRecord(
                user,
                "run",
                "device",
                started,
                started,
                started + 0.01,
                200,
                request_id,
                trace_id=request_id,
            )
        )
    for event in events:
        accounting.server_event(event)
    return accounting


def test_import_retains_original_server_clock_and_peak_users():
    accounting = _accounting(_events())
    evidence = accounting.evidence()
    assert evidence["trusted"]
    assert [r["monotonic"] for r in evidence["server_records"]] == [10, 11, 14, 15]
    execution = evidence["server_executing"]
    assert execution["in_flight"] == 1
    assert execution["at_monotonic"] == 11
    assert execution["request_ids_at_peak"] == ["request-1"]
    assert evidence["peaks"]["client"]["request_ids_at_peak"] == ["request-1"]
    assert execution["user_ids_at_peak"] == ["actor-1"]
    assert evidence["server_queued"] is None
    assert evidence["application_queue"] == "absent"
    assert evidence["listen_backlog"] == "unmeasured"


@pytest.mark.parametrize(
    "damage",
    [
        "missing_start",
        "missing_end",
        "duplicate_start",
        "duplicate_end",
        "out_of_order",
        "wrong_result",
        "missing_clock",
        "wrong_process",
        "rewritten_seconds",
        "empty_trace",
    ],
)
def test_execution_event_damage_cannot_be_trusted(damage):
    events = deepcopy(_events())
    if damage == "missing_start":
        events.pop(1)
    elif damage == "missing_end":
        events.pop(2)
    elif damage == "duplicate_start":
        events.insert(2, deepcopy(events[1]))
    elif damage == "duplicate_end":
        events.insert(3, deepcopy(events[2]))
    elif damage == "out_of_order":
        events[1], events[2] = events[2], events[1]
    elif damage == "wrong_result":
        events[2]["result"] = "internal"
    elif damage == "missing_clock":
        del events[1]["monotonic_ns"]
    elif damage == "wrong_process":
        events[2]["process_id"] = 999
    elif damage == "empty_trace":
        events[1]["trace_id"] = ""
    accounting = _accounting(events)
    if damage == "rewritten_seconds":
        accounting.server_records[1].monotonic += 2
    assert accounting.conservation_report()
    assert accounting.evidence()["trusted"] is False


def test_unrelated_server_processes_are_not_swept_as_one_clock():
    accounting = _accounting(_events())
    started = accounting.client_start()
    accounting.client_complete(
        ClientRecord(
            "actor-2",
            "run",
            "device",
            started,
            started,
            started + 0.01,
            200,
            "request-2",
            trace_id="request-2",
        )
    )
    for event in _events("request-2", process_id=999):
        accounting.server_event(event)
    assert (
        "multiple server process clocks cannot share an interval sweep"
        in accounting.conservation_report()
    )
    evidence = accounting.evidence()
    assert evidence["trusted"] is False
    assert evidence["peaks"]["server"]["in_flight"] == 0
    assert evidence["server_executing"]["in_flight"] == 0
