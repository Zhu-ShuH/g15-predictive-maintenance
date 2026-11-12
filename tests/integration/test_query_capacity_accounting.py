"""Real D0 HTTP evidence and falsifiable harness checks for Issue #136.

The integration run proves response, paging and accounting quality. Reaching a
10/50-request target is a separate measured outcome, never inferred from the
number submitted or made into a scheduling-dependent CI assertion.
"""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import inspect
from io import BytesIO
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from g15.query import application as query_application  # noqa: E402
from tools.query_load import runner  # noqa: E402
from tools.query_load.accounting import (  # noqa: E402
    ClientRecord,
    RunAccounting,
    SERVER_COMPLETED,
    SERVER_RECEIVED,
)
from tools.query_load.client import QueryClient  # noqa: E402
from tools.query_load.d0_fixture import (  # noqa: E402
    D0_ACTOR_REF,
    D0_DEVICE_COUNT,
    D0_WINDOWS_PER_DEVICE,
)
from tools.query_load.driver import verify_all  # noqa: E402
from tools.query_load.evidence import write_evidence  # noqa: E402
from tools.query_load.paging import (  # noqa: E402
    PagingWalk,
    run_paging,
    verify_paging_coverage,
)


@pytest.fixture(scope="module")
def measured_d0(tmp_path_factory):
    root = tmp_path_factory.mktemp("query-capacity")
    report = runner.run_measurement(root / "runs")
    # Retain the actual target miss or failure, before any test assertions.
    write_evidence(root / "evidence", report)
    return report, sorted((root / "runs").glob("*/artifacts/results.sqlite"))


def test_d0_fixture_shape(measured_d0) -> None:
    _report, db_paths = measured_d0
    assert len(db_paths) == D0_DEVICE_COUNT
    for path in db_paths:
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM feature_windows"
            ).fetchone() == (D0_WINDOWS_PER_DEVICE,)
            assert connection.execute("SELECT COUNT(*) FROM alarms").fetchone() == (1,)
            assert connection.execute(
                "SELECT COUNT(*) FROM alarm_states"
            ).fetchone() == (1,)


def test_real_http_quality_and_separate_measured_target(measured_d0) -> None:
    report, _db_paths = measured_d0
    assert report["quality_passed"], report["checks"]
    assert not any(report["checks"].values())
    assert report["gate0_passed"] is False
    accounting = report["accounting"]
    assert accounting["trusted"]
    assert len(accounting["client_records"]) == 30
    assert accounting["distinct_users_total"] == 10
    actors = {
        row["actor_ref"]
        for row in report["application_events"]
        if row["stage"] == "authz_checked"
    }
    assert actors == {row["user_id"] for row in accounting["client_records"]}
    assert len(actors) == 10
    assert accounting["server_queued"] is None
    assert accounting["server_executing"]["in_flight"] > 0
    assert accounting["server_executing"]["distinct_users_at_peak"] > 0
    assert accounting["latency_seconds"]["count"] == 30
    assert accounting["latency_seconds"]["p99"] is not None
    peaks = accounting["peaks"]
    reached = all(
        peaks[side]["in_flight"] >= 10 and peaks[side]["distinct_users_at_peak"] >= 10
        for side in ("client", "server")
    )
    assert report["target_reached"] is reached
    assert report["exit_code"] == (0 if reached else 1)
    assert report["paging"]["counts"] == {
        "windows": {"raw": 100, "consumed": 100, "discarded": 0},
        "alarms": {"raw": 10, "consumed": 1, "discarded": 9},
    }
    assert report["paging"]["raw_duplicate_free_claim"] is False
    assert report["paging"]["consumed_coverage_problems"] == []
    assert (
        "paging alarms total 10 != 1" in report["paging"]["raw_coverage_observations"]
    )


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("repeat_page", "windows total 110 != 100"),
        ("missing_page", "windows total 90 != 100"),
        ("replace_window", "windows set mismatch"),
        ("reorder_windows", "windows out of descending order"),
        ("repeat_alarm", "alarms total 2 != 1"),
        ("wrong_alarm_key", "alarms set mismatch"),
        ("missing_alarm", "alarms total 0 != 1"),
    ],
)
def test_paging_coverage_rejects_count_key_and_order_damage(
    measured_d0, damage, expected
) -> None:
    report, db_paths = measured_d0
    pages = deepcopy(report["paging"]["consumed_responses"])
    first = pages[0][1]
    if damage == "repeat_page":
        pages.append(deepcopy(pages[-1]))
    elif damage == "missing_page":
        pages.pop()
    elif damage == "replace_window":
        first["windows"]["items"][0] = deepcopy(first["windows"]["items"][1])
    elif damage == "reorder_windows":
        first["windows"]["items"][:2] = reversed(first["windows"]["items"][:2])
    elif damage == "repeat_alarm":
        first["alarms"]["items"].append(deepcopy(first["alarms"]["items"][0]))
    elif damage == "wrong_alarm_key":
        first["alarms"]["items"][0]["rule_id"] = "not-committed"
    else:
        first["alarms"]["items"].clear()
    problems = verify_paging_coverage(pages)
    assert any(expected in problem for problem in problems), problems
    if damage in {"repeat_page", "missing_page", "missing_alarm"}:
        # Individually valid pages cannot establish cross-page completeness.
        assert verify_all(pages, db_paths) == []
    else:
        # The row oracle also rejects duplicates/order damage within one page.
        assert verify_all(pages, db_paths)


def test_discarded_alarm_is_checked_in_raw_response(measured_d0) -> None:
    report, db_paths = measured_d0
    responses = deepcopy(report["paging"]["raw_responses"])
    responses[1][1]["alarms"]["items"][0]["reason"] = "fabricated replay"
    replay = iter(responses)
    client = SimpleNamespace(query=lambda *args, **kwargs: next(replay)[:2])
    walk = run_paging(client, 0, db_paths)
    assert walk.raw[1][1]["alarms"]["items"][0]["reason"] == "fabricated replay"
    assert walk.consumed[1][1]["alarms"]["items"] == []
    assert verify_all(walk.consumed, db_paths) == []
    assert verify_paging_coverage(walk.consumed) == []
    verdict = runner.validate_run([], walk, db_paths, RunAccounting(), [])
    assert verdict["quality_passed"] is False
    assert any(
        "raw[1]" in problem and "reason mismatch" in problem
        for problem in verdict["checks"]["raw_oracle"]
    )
    # The consumed view must never mutate a retained raw response.
    walk.consumed[0][1]["windows"]["items"][0]["health_score"] = -1
    assert walk.raw[0][1]["windows"]["items"][0]["health_score"] >= 0
    assert walk.evidence()["counts"]["alarms"]["discarded"] == 9


def test_cli_uses_shared_coverage_and_persists_failure(tmp_path, monkeypatch) -> None:
    original_paging = runner.run_paging

    def replay_consumed_alarm(*args, **kwargs):
        walk = original_paging(*args, **kwargs)
        items = walk.consumed[0][1]["alarms"]["items"]
        items.append(deepcopy(items[0]))
        return walk

    monkeypatch.setattr(runner, "run_paging", replay_consumed_alarm)
    output = tmp_path / "failed-evidence"
    assert runner.main(["--serial", "1", "--output", str(output)]) == 1
    saved = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert saved["quality_passed"] is False
    assert saved["checks"]["raw_oracle"] == []
    assert saved["checks"]["consumed_coverage"] == ["paging alarms total 2 != 1"]
    assert saved["paging"]["counts"]["alarms"] == {
        "raw": 10,
        "consumed": 2,
        "discarded": 9,
    }
    assert len(saved["accounting"]["client_records"]) == 21
    assert saved["exit_code"] == 1


@pytest.mark.parametrize("damage", ["duplicate_completion", "unknown_request"])
def test_accounting_rejects_duplicate_completion_or_unknown_id(damage) -> None:
    accounting = RunAccounting()
    started = accounting.client_start()

    def emit(request_id, stage):
        accounting.server_event(
            {
                "request_id": request_id,
                "trace_id": request_id,
                "monotonic_ns": time.perf_counter_ns(),
                "process_id": 1,
                "stage": stage,
                "result": (
                    "received"
                    if stage == SERVER_RECEIVED
                    else "started" if stage == "server_executing" else "success"
                ),
            }
        )

    emit("request-1", SERVER_RECEIVED)
    emit("request-1", "server_executing")
    emit("request-1", "server_execution_completed")
    emit("request-1", SERVER_COMPLETED)
    accounting.client_complete(
        ClientRecord(
            D0_ACTOR_REF,
            "run",
            "device",
            started,
            started,
            time.perf_counter(),
            200,
            "request-1",
            trace_id="request-1",
        )
    )
    assert accounting.conservation_report() == []
    if damage == "duplicate_completion":
        emit("request-1", SERVER_COMPLETED)
        expected = "response_completed request_id 'request-1' occurs 2 times"
    else:
        emit("unknown", SERVER_RECEIVED)
        emit("unknown", "server_executing")
        emit("unknown", "server_execution_completed")
        emit("unknown", SERVER_COMPLETED)
        expected = "client/server received request_id counts do not match"
    assert expected in accounting.conservation_report()
    assert accounting.evidence()["trusted"] is False


@pytest.mark.parametrize(
    ("failure", "status", "error"),
    [
        ("invalid_json", 200, "invalid_json"),
        ("timeout", 0, "TimeoutError"),
        ("nan", 200, "invalid_json"),
        ("infinity", 200, "invalid_json"),
        ("overflow", 200, "invalid_json"),
    ],
)
def test_client_decode_and_transport_failures_complete_accounting(
    monkeypatch, failure, status, error
) -> None:
    raw_response = {
        "invalid_json": b"not-json",
        "timeout": b"",
        "nan": b'{"score": NaN}',
        "infinity": b'{"score": Infinity}',
        "overflow": b'{"score": 1e999}',
    }[failure]

    def open_response(*args, **kwargs):
        if failure == "timeout":
            raise TimeoutError("private transport details")
        response = BytesIO(raw_response)
        response.status = 200
        return response

    accounting = RunAccounting()
    client = QueryClient(
        "http://127.0.0.1:1", "private-token", D0_ACTOR_REF, accounting
    )
    monkeypatch.setattr(client._opener, "open", open_response)
    assert client.query("run", "device") == (status, {})
    evidence = accounting.evidence()
    assert len(evidence["client_records"]) == 1
    record = evidence["client_records"][0]
    assert record["error"] == error
    assert record["started_monotonic"] <= record["waiting_monotonic"]
    assert record["waiting_monotonic"] <= record["completed_monotonic"]
    assert base64.b64decode(record["response_bytes_base64"]) == raw_response
    json.dumps(evidence, allow_nan=False)
    assert evidence["latency_seconds"]["count"] == 1
    assert not any(
        "client_in_flight nonzero" in problem
        for problem in evidence["conservation_problems"]
    )
    assert evidence["trusted"] is False
    assert "private-" not in json.dumps(evidence)
    assert "private transport details" not in json.dumps(evidence)


def test_evidence_refuses_to_overwrite_previous_attempt(tmp_path, measured_d0) -> None:
    report, _db_paths = measured_d0
    output = tmp_path / "retained"
    write_evidence(output, report)
    original = (output / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        write_evidence(output, {"quality_passed": False})
    assert (output / "report.json").read_bytes() == original


def test_query_plan_evidence_matches_application_sql_and_live_database(
    measured_d0,
) -> None:
    report, db_paths = measured_d0
    plans = report["query_plans"]
    assert {row["case"] for row in plans} == {
        "window_high_water",
        "alarm_high_water",
        "windows_first_page",
        "windows_continuation_probe",
        "alarms_first_page",
        "alarms_continuation_branch_probe",
        "states",
    }
    source = Path(query_application.__file__)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    databases = {path.parent.parent.name: path for path in db_paths}
    for row in plans:
        assert row["source_path"] == "src/g15/query/application.py"
        assert row["source_sha256"] == digest
        assert row["evidence_kind"] == "actual_application_select_helper_probe"
        function = getattr(query_application, row["source_function"])
        lines, first_line = inspect.getsourcelines(function)
        assert first_line <= row["source_line"] < first_line + len(lines)
        with sqlite3.connect(databases[row["run_id"]]) as connection:
            expected = connection.execute(
                "EXPLAIN QUERY PLAN " + row["sql"], row["parameters"]
            ).fetchall()
        assert row["plan"] == [list(item) for item in expected]
        assert row["plan"]


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("empty_windows", "windows first-page keys/order mismatch"),
        ("empty_alarms", "alarms first-page keys/order mismatch"),
        ("reordered_first_page", "windows first-page keys/order mismatch"),
        ("wrong_device", "response run/device identity mismatch"),
        ("missing_states", "states key coverage mismatch"),
        ("malformed_state", "malformed state object"),
    ],
)
def test_oracle_rejects_incomplete_first_page_identity_and_state(
    measured_d0, damage, expected
) -> None:
    report, db_paths = measured_d0
    result = deepcopy(report["serial_responses"][0])
    body = result[1]
    if damage == "empty_windows":
        body["windows"]["items"].clear()
    elif damage == "empty_alarms":
        body["alarms"]["items"].clear()
    elif damage == "reordered_first_page":
        body["windows"]["items"][:2] = reversed(body["windows"]["items"][:2])
    elif damage == "wrong_device":
        body["device_id"] = "wrong-device"
    elif damage == "missing_states":
        body["states"].clear()
    else:
        body["states"][0]["state"] = None
    # Identity and complete state coverage apply even outside first-page mode.
    first_page = damage in {"empty_windows", "empty_alarms", "reordered_first_page"}
    problems = verify_all([result], db_paths, first_page=first_page)
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize("damage", ["failed_completion", "extra_rejected", "reordered"])
def test_success_response_requires_exact_application_event_sequence(
    measured_d0, damage
) -> None:
    report, db_paths = measured_d0
    accounting = RunAccounting()
    for row in report["accounting"]["client_records"]:
        accounting.client_start()
        accounting.client_complete(ClientRecord(**row))
    for row in report["accounting"]["server_records"]:
        accounting.server_event(row)
    events = deepcopy(report["application_events"])
    paging = PagingWalk(
        raw=report["paging"]["raw_responses"],
        consumed=report["paging"]["consumed_responses"],
        discards=report["paging"]["discards"],
    )
    results = report["serial_responses"] + report["concurrent_responses"]
    assert runner.validate_run(results, paging, db_paths, accounting, events)[
        "quality_passed"
    ]
    first_id = events[0]["request_id"]
    completed = next(
        row
        for row in events
        if row["request_id"] == first_id and row["stage"] == "query_completed"
    )
    if damage == "failed_completion":
        completed["result"] = "error"
    elif damage == "extra_rejected":
        rejected = dict(completed, stage="rejected", result="denied")
        events.append(rejected)
    else:
        events[0], events[1] = events[1], events[0]
    verdict = runner.validate_run(results, paging, db_paths, accounting, events)
    assert verdict["quality_passed"] is False
    assert verdict["checks"]["request_log"] == [
        f"{first_id}: application stage/result sequence mismatch"
    ]
    assert all(
        not problems
        for name, problems in verdict["checks"].items()
        if name != "request_log"
    )


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("exhausted_empty_alarm", "alarms first-page keys/order mismatch"),
        ("exhausted_duplicate_alarm", "alarms first-page keys/order mismatch"),
        ("active_duplicate_window", "windows duplicate keys within page"),
        ("active_reordered_window", "windows keys out of descending order"),
    ],
)
def test_wire_paging_checks_exhausted_first_pages_and_active_keys(
    tmp_path, monkeypatch, damage, expected
) -> None:
    """Inject a wrong real HTTP body before the client records its raw bytes."""
    from dataclasses import replace

    original = query_application.HealthQueryApplication.query_authorized
    damaged = False

    def wrong_response(self, *args, **kwargs):
        nonlocal damaged
        response = original(self, *args, **kwargs)
        if damaged or not kwargs.get("windows_cursor"):
            return response
        damaged = True
        body = deepcopy(response.body)
        if damage == "exhausted_empty_alarm":
            body["alarms"]["items"].clear()
        elif damage == "exhausted_duplicate_alarm":
            body["alarms"]["items"].append(deepcopy(body["alarms"]["items"][0]))
        elif damage == "active_duplicate_window":
            body["windows"]["items"][0] = deepcopy(body["windows"]["items"][1])
        else:
            body["windows"]["items"][:2] = reversed(body["windows"]["items"][:2])
        return replace(response, body=body)

    monkeypatch.setattr(
        query_application.HealthQueryApplication, "query_authorized", wrong_response
    )
    report = runner.run_measurement(tmp_path / "runs")
    write_evidence(tmp_path / "evidence", report)
    assert damaged
    assert report["quality_passed"] is False
    assert any(expected in row for row in report["checks"]["raw_oracle"])
    raw_body = report["paging"]["raw_responses"][1][1]
    record = next(
        row
        for row in report["accounting"]["client_records"]
        if row["server_request_id"] == raw_body["request_id"]
    )
    assert json.loads(base64.b64decode(record["response_bytes_base64"])) == raw_body
    if damage.startswith("exhausted_"):
        # A clean consumed view cannot excuse a wrong discarded HTTP page.
        assert report["checks"]["consumed_coverage"] == []
        expected_raw = 9 if damage == "exhausted_empty_alarm" else 11
        assert report["paging"]["counts"]["alarms"] == {
            "raw": expected_raw,
            "consumed": 1,
            "discarded": expected_raw - 1,
        }
    else:
        assert report["checks"]["consumed_coverage"]


@pytest.mark.parametrize(
    "damage",
    [
        "missing_response",
        "duplicate_replaces_missing",
        "extra_response",
        "missing_raw_page",
        "wrong_response_id",
        "changed_retained_body",
    ],
)
def test_response_evidence_must_match_every_accounted_request(measured_d0, damage):
    report, db_paths = measured_d0
    accounting = RunAccounting()
    for row in report["accounting"]["client_records"]:
        accounting.client_start()
        accounting.client_complete(ClientRecord(**row))
    for row in report["accounting"]["server_records"]:
        accounting.server_event(row)
    paging = PagingWalk(
        raw=deepcopy(report["paging"]["raw_responses"]),
        consumed=deepcopy(report["paging"]["consumed_responses"]),
        discards=deepcopy(report["paging"]["discards"]),
    )
    results = deepcopy(report["serial_responses"] + report["concurrent_responses"])
    assert runner.validate_run(
        results, paging, db_paths, accounting, report["application_events"]
    )["quality_passed"]
    if damage == "missing_response":
        results.pop()
    elif damage == "duplicate_replaces_missing":
        results[-1] = deepcopy(results[0])
    elif damage == "extra_response":
        results.append(deepcopy(results[0]))
    elif damage == "missing_raw_page":
        paging.raw.pop(1)
    elif damage == "wrong_response_id":
        results[0][1]["request_id"] = "not-an-accounted-response"
    else:
        results[0][1]["not_received_from_server"] = True
    verdict = runner.validate_run(
        results, paging, db_paths, accounting, report["application_events"]
    )
    assert verdict["quality_passed"] is False
    assert verdict["checks"]["response_accounting"]
    if damage == "changed_retained_body":
        # An unknown field violates projection even before byte reconciliation.
        assert verdict["checks"]["raw_oracle"]
    assert all(
        not problems
        for name, problems in verdict["checks"].items()
        if name
        not in (
            {"response_accounting", "raw_oracle"}
            if damage == "changed_retained_body"
            else {"response_accounting"}
        )
    )


@pytest.mark.parametrize("when", ["before_requests", "after_requests"])
def test_storage_failure_always_persists_partial_measurement(
    tmp_path, monkeypatch, when
) -> None:
    from g15.storage.database import DatabaseError, connect_readonly

    if when == "before_requests":

        def unavailable_store(_root):
            raise DatabaseError("synthetic storage failure")

        monkeypatch.setattr(runner, "build_d0_runs", unavailable_store)
    else:

        def unavailable_plan(_paths):
            return connect_readonly(tmp_path / "missing-store.sqlite")

        monkeypatch.setattr(runner, "query_plans", unavailable_plan)
    output = tmp_path / "failed-storage-evidence"
    assert runner.main(["--serial", "1", "--output", str(output)]) == 1
    saved = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert saved["runner_failure"] == "DatabaseError"
    assert saved["quality_passed"] is False
    assert saved["exit_code"] == 1
    records = saved["accounting"]["client_records"]
    assert len(records) == (0 if when == "before_requests" else 21)
    if when == "after_requests":
        assert all(record["response_bytes_base64"] for record in records)


@pytest.mark.parametrize(
    "damage", ["unknown_rejected", "failed_response_completed", "extra_rejected"]
)
def test_accounting_rejects_unmatched_or_failed_server_terminal(measured_d0, damage):
    from tools.query_load.accounting import ServerRecord

    report, db_paths = measured_d0
    rows = deepcopy(report["accounting"]["server_records"])
    completed = next(row for row in rows if row["stage"] == SERVER_COMPLETED)
    if damage == "unknown_rejected":
        rows.append(
            dict(
                completed,
                request_id="unknown-rejected-request",
                trace_id="unknown-rejected-request",
                stage="rejected",
                result="overloaded",
            )
        )
        expected = "server events contain unknown request_ids"
    elif damage == "failed_response_completed":
        completed["result"] = "connection_error"
        expected = "server stage/result sequence mismatch"
    else:
        rows.append(dict(completed, stage="rejected", result="internal"))
        expected = "server stage/result sequence mismatch"
    accounting = RunAccounting(
        client_records=[
            ClientRecord(**row) for row in report["accounting"]["client_records"]
        ],
        server_records=[ServerRecord(**row) for row in rows],
        _client_started=len(report["accounting"]["client_records"]),
    )
    paging = PagingWalk(
        raw=report["paging"]["raw_responses"],
        consumed=report["paging"]["consumed_responses"],
        discards=report["paging"]["discards"],
    )
    verdict = runner.validate_run(
        report["serial_responses"] + report["concurrent_responses"],
        paging,
        db_paths,
        accounting,
        report["application_events"],
    )
    assert verdict["quality_passed"] is False
    assert any(expected in row for row in verdict["checks"]["conservation"])
    assert all(
        not problems
        for name, problems in verdict["checks"].items()
        if name != "conservation"
    )
    evidence = accounting.evidence()
    assert evidence["trusted"] is False
    assert evidence["server_records"] == rows


def test_accounting_diagnostic_failure_retains_original_records(tmp_path, monkeypatch):
    def broken_diagnostics(_self):
        raise ArithmeticError("safe synthetic diagnostic failure")

    monkeypatch.setattr(RunAccounting, "evidence", broken_diagnostics)
    output = tmp_path / "failed-accounting-evidence"
    assert runner.main(["--serial", "1", "--output", str(output)]) == 1
    saved = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert saved["runner_failure"] == "ArithmeticError"
    assert saved["quality_passed"] is False
    assert saved["target_reached"] is False
    assert saved["exit_code"] == 1
    assert saved["accounting"]["trusted"] is False
    assert saved["accounting"]["incomplete"] is True
    assert len(saved["accounting"]["client_records"]) == 21
    assert len(saved["accounting"]["server_records"]) == 84
    assert len(saved["paging"]["raw_responses"]) == 10
    assert all(
        record["response_bytes_base64"]
        for record in saved["accounting"]["client_records"]
    )


_WIRE_CONTRACT_DAMAGE = (
    "missing_state_version",
    "wrong_state_version",
    "wrong_state_versions",
    "wrong_feature_versions",
    "wrong_query_version",
    "numeric_manifest_version",
    "boolean_state_counter",
    "float_state_counter",
    "float_window_count",
    "missing_nullable_state",
    "leaked_state_audit",
    "leaked_nested_audit",
    "unknown_top_level",
    "wrong_windows_sort",
    "wrong_alarms_sort",
    "wrong_windows_page_size",
    "wrong_alarms_page_size",
)


def _damage_query_contract(body, damage):
    if damage == "missing_state_version":
        del body["states"][0]["state_schema_version"]
    elif damage == "wrong_state_version":
        body["states"][0]["state_schema_version"] = "wrong"
    elif damage == "wrong_state_versions":
        body["versions"]["state_schema_versions"] = ["wrong"]
    elif damage == "wrong_feature_versions":
        body["versions"]["features_versions"] = ["wrong"]
    elif damage == "wrong_query_version":
        body["query_contract_version"] = "wrong"
    elif damage == "numeric_manifest_version":
        body["versions"]["manifest_schema_version"] = 2.0
    elif damage == "boolean_state_counter":
        body["states"][0]["state"]["consecutive_breaches"] = False
    elif damage == "float_state_counter":
        body["states"][0]["state"]["normal_windows"] = 100.0
    elif damage == "float_window_count":
        body["windows"]["items"][0]["sample_count"] = 10.0
    elif damage == "missing_nullable_state":
        del body["states"][0]["state"]["recovered_at_seq"]
    elif damage == "leaked_state_audit":
        body["states"][0]["reset_event"] = {
            "reason": "operator-private-reset-reason",
            "previous_state_json": "private-reset-envelope",
        }
    elif damage == "leaked_nested_audit":
        body["states"][0]["state"]["reset_history"] = [
            {"previous_state_json": "private-reset-envelope"}
        ]
    elif damage in {"wrong_windows_sort", "wrong_alarms_sort"}:
        stream = "windows" if damage == "wrong_windows_sort" else "alarms"
        body[stream]["sort"] = "ascending"
    elif damage in {"wrong_windows_page_size", "wrong_alarms_page_size"}:
        stream = "windows" if damage == "wrong_windows_page_size" else "alarms"
        body[stream]["page_size"] = 50
    else:
        body["unapproved_field"] = "unexpected"


@pytest.mark.parametrize("damage", _WIRE_CONTRACT_DAMAGE)
@pytest.mark.parametrize("entrypoint", ["measurement", "cli", "opt_in"])
def test_wire_contract_damage_fails_shared_entrypoints(
    tmp_path, monkeypatch, damage, entrypoint
):
    """Inject an invalid real HTTP response before raw bytes are retained."""
    from dataclasses import replace
    import runpy

    original = query_application.HealthQueryApplication.query_authorized
    damaged = False

    def wrong_response(self, *args, **kwargs):
        nonlocal damaged
        response = original(self, *args, **kwargs)
        if damaged:
            return response
        damaged = True
        body = deepcopy(response.body)
        _damage_query_contract(body, damage)
        return replace(response, body=body)

    monkeypatch.setattr(
        query_application.HealthQueryApplication, "query_authorized", wrong_response
    )
    output = tmp_path / "evidence"
    if entrypoint == "measurement":
        report = runner.run_measurement(tmp_path / "runs", serial=1)
        write_evidence(output, report)
    elif entrypoint == "cli":
        assert runner.main(["--serial", "1", "--output", str(output)]) == 1
    else:
        monkeypatch.setenv("G15_RUN_QUERY_LOAD", "1")
        monkeypatch.setenv("G15_QUERY_CONCURRENT", "10")
        opt_in = runpy.run_path(
            str(_REPO_ROOT / "tests/performance/query/test_query_load.py")
        )["test_gate0_low_load_query_measurement"]
        with pytest.raises(AssertionError):
            opt_in(tmp_path)
    saved = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert damaged
    assert saved["quality_passed"] is False
    assert saved["checks"]["raw_oracle"]
    assert saved["exit_code"] == 1
    # These are actual bytes received from the service; reconciliation must pass.
    assert saved["checks"]["response_accounting"] == []
    first = saved["serial_responses"][0][1]
    record = next(
        row
        for row in saved["accounting"]["client_records"]
        if row["server_request_id"] == first["request_id"]
    )
    assert json.loads(base64.b64decode(record["response_bytes_base64"])) == first


def test_oracle_accepts_integer_health_score_and_explicit_null_state(measured_d0):
    report, db_paths = measured_d0
    result = deepcopy(report["serial_responses"][0])
    first = result[1]["windows"]["items"][0]
    assert first["health_score"] == 55.0
    first["health_score"] = 55
    assert result[1]["states"][0]["state"]["recovered_at_seq"] is None
    assert verify_all([result], db_paths, first_page=True) == []
