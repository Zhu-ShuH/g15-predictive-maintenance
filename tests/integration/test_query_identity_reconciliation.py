"""Three-way identity reconciliation over copies of one real HTTP measurement."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.query_load.accounting import (  # noqa: E402
    ClientRecord,
    RunAccounting,
    ServerRecord,
)
from tools.query_load.evidence import write_evidence  # noqa: E402
from tools.query_load.paging import PagingWalk  # noqa: E402
from tools.query_load.runner import run_measurement, validate_run  # noqa: E402


@pytest.fixture(scope="module")
def identity_measurement(tmp_path_factory):
    root = tmp_path_factory.mktemp("query-identity")
    report = run_measurement(root / "runs", serial=1)
    write_evidence(root / "evidence", report)
    assert report["quality_passed"], report["checks"]
    return report, sorted((root / "runs").glob("*/artifacts/results.sqlite"))


def _validate_retained(report, db_paths):
    records = report["accounting"]["client_records"]
    accounting = RunAccounting(
        client_records=[ClientRecord(**row) for row in records],
        server_records=[
            ServerRecord(**row) for row in report["accounting"]["server_records"]
        ],
        _client_started=len(records),
    )
    paging = PagingWalk(
        raw=report["paging"]["raw_responses"],
        consumed=report["paging"]["consumed_responses"],
        discards=report["paging"]["discards"],
        oracle_problems=report["paging"]["raw_oracle_problems"],
        traversal_problems=report["paging"]["traversal_problems"],
    )
    return validate_run(
        report["serial_responses"] + report["concurrent_responses"],
        paging,
        db_paths,
        accounting,
        report["application_events"],
    )


def test_unchanged_real_identity_measurement_passes(identity_measurement):
    report, db_paths = identity_measurement
    verdict = _validate_retained(report, db_paths)
    assert verdict["quality_passed"]
    assert not any(verdict["checks"].values())


_IDENTITY_DAMAGE = (
    [
        ("transport", stage, field, operation)
        for stage in ("server_execution_completed", "response_completed")
        for field in ("actor_ref", "permission_domain")
        for operation in ("replace", "remove")
    ]
    + [
        ("transport", "server_received", "actor_ref", "replace"),
        ("transport", "server_executing", "permission_domain", "replace"),
    ]
    + [
        ("application", "authz_checked", field, operation)
        for field in ("actor_ref", "permission_domain")
        for operation in ("replace", "remove")
    ]
    + [
        ("application", "db_opened", "actor_ref", "replace"),
        ("application", "query_started", "permission_domain", "replace"),
        ("application", "query_completed", "actor_ref", "replace"),
        ("application", "query_completed", "permission_domain", "replace"),
    ]
)


@pytest.mark.parametrize("side,stage,field,operation", _IDENTITY_DAMAGE)
def test_identity_damage_fails_without_rewriting_responses(
    identity_measurement, side, stage, field, operation
):
    original, db_paths = identity_measurement
    report = deepcopy(original)
    request_id = report["accounting"]["client_records"][0]["server_request_id"]
    rows = (
        report["accounting"]["server_records"]
        if side == "transport"
        else report["application_events"]
    )
    row = next(
        row for row in rows if row["request_id"] == request_id and row["stage"] == stage
    )
    if operation == "remove":
        del row[field]
    else:
        row[field] = "different-actor" if field == "actor_ref" else "different-domain"
    # Only identity evidence changes. Received bodies and original byte records
    # are identical to the real measurement and still reconcile successfully.
    assert report["serial_responses"] == original["serial_responses"]
    assert report["concurrent_responses"] == original["concurrent_responses"]
    assert report["paging"] == original["paging"]
    assert (
        report["accounting"]["client_records"]
        == original["accounting"]["client_records"]
    )
    verdict = _validate_retained(report, db_paths)
    assert verdict["quality_passed"] is False
    assert verdict["checks"]["request_log"]
    assert all(
        not problems
        for name, problems in verdict["checks"].items()
        if name != "request_log"
    )
