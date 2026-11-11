"""Gate 0 capacity accounting integration test (Issue #136).

Drives the low-load harness (tools/query_load) end to end over a real HTTP
server backed by the D0 committed store, then asserts two Gate 0 invariants:

1. every 200 response the harness drove matches the independent SQLite oracle
   item by item (run / versions / windows / alarms / states);
2. the trusted accounting conserves: client and server in-flight return to
   zero, server_received == client total, and client/server request ids match.

This is the Gate 0 acceptance shape: D0 store, one user, ten requests, plus a
10-way concurrent wave and a full paging walk, all without handler sleep or
artificial throttling.
"""

from __future__ import annotations

import secrets
import sqlite3
import sys
from pathlib import Path

import pytest

# tools/ is a namespace package at the repository root; make it importable
# regardless of how pytest was launched (python -m pytest vs pytest entry point).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.query_load.accounting import RunAccounting  # noqa: E402
from tools.query_load.client import QueryClient  # noqa: E402
from tools.query_load.d0_fixture import (  # noqa: E402
    D0_DEVICE_COUNT,
    D0_WINDOWS_PER_DEVICE,
    build_d0_runs,
    device_id,
    device_run_id,
)
from tools.query_load.driver import (  # noqa: E402
    BEARER_AUDIENCE,
    BEARER_ISSUER,
    build_application,
    run_concurrent,
    run_paging,
    run_serial,
    start_server,
    verify_all,
    verify_paging_coverage,
)
from tools.query_load.oracle import verify_response  # noqa: E402
from g15.query.auth import issue_token  # noqa: E402


@pytest.fixture
def d0_store(tmp_path: Path) -> list[Path]:
    return build_d0_runs(tmp_path / "runs")


def test_d0_fixture_shape(d0_store: list[Path]) -> None:
    assert len(d0_store) == D0_DEVICE_COUNT
    for db_path in d0_store:
        assert db_path.exists()
        connection = sqlite3.connect(str(db_path))
        try:
            (windows,) = connection.execute(
                "SELECT COUNT(*) FROM feature_windows"
            ).fetchone()
            (alarms,) = connection.execute("SELECT COUNT(*) FROM alarms").fetchone()
            (states,) = connection.execute(
                "SELECT COUNT(*) FROM alarm_states"
            ).fetchone()
        finally:
            connection.close()
        assert windows == D0_WINDOWS_PER_DEVICE
        assert alarms == 1
        assert states == 1


def test_serial_concurrent_paging_reconcile_and_oracle_clean(
    d0_store: list[Path],
) -> None:
    runs_root = d0_store[0].parents[2]
    bearer_key = secrets.token_bytes(32)
    cursor_key = secrets.token_bytes(32)
    application, _events = build_application(runs_root, bearer_key, cursor_key)
    accounting = RunAccounting()
    server, _thread, base_url = start_server(application, accounting, max_workers=16)
    token = issue_token(
        key=bearer_key,
        issuer=BEARER_ISSUER,
        audience=BEARER_AUDIENCE,
        subject="subject-d0",
        now=1_900,
    )
    client = QueryClient(base_url, token, "user-d0", accounting)
    try:
        serial = run_serial(client, 10, d0_store)
        concurrent = run_concurrent(client, 10, d0_store)
        paging = run_paging(client, 0, d0_store)
    finally:
        server.shutdown()
        server.server_close()

    all_results = serial + concurrent + paging
    assert all((status == 200 for status, _body, _run in all_results))
    assert verify_all(all_results, d0_store) == []
    assert verify_paging_coverage(paging) == []
    assert accounting.conservation_report() == []
    # The concurrent wave must actually reach 10 in flight on the client side.
    assert accounting.peak_client_in_flight >= 10


def test_oracle_detects_window_divergence(d0_store: list[Path]) -> None:
    """The oracle must flag a fabricated window, proving it really checks data."""
    db_path = d0_store[0]
    run_id = device_run_id(0)
    device = device_id(0)
    body = {
        "run_id": run_id,
        "device_id": device,
        "run_status": "complete",
        "versions": {
            "manifest_schema_version": "2.0",
            "run_scoring_version": "score-d0-1",
        },
        "windows": {
            "items": [
                {
                    "start_seq": -1,
                    "end_seq_exclusive": 9,
                    "health_score": 100.0,
                    "features_version": "1",
                    "sample_count": 10,
                    "sample_rate_hz": 100,
                    "scoring_version": "score-d0-1",
                    "rule_id": "health-d0",
                }
            ]
        },
        "alarms": {"items": []},
        "states": [],
    }
    problems = verify_response(body, db_path, run_id, device, 10)
    assert problems, "oracle must flag the fabricated start_seq"
    assert any("start_seq -1 not committed" in p for p in problems)
