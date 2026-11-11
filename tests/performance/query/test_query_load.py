"""Gate 0 low-load performance measurement (Issue #136).

Opt-in because a shared CI runner is not a valid measurement host.  Enable with::

    G15_RUN_QUERY_LOAD=1 .venv/Scripts/python.exe -m pytest \
        -q -s tests/performance/query/test_query_load.py

Measures the real read-only query path over HTTP on the D0 store (10 devices x
100 windows): serial 10, concurrent 10 in flight, and a full paging walk.  It
reports per-wave elapsed time, client/server peak in-flight, and a coarse
latency distribution, and asserts the oracle and accounting stay clean.

This is a low-load smoke measurement only.  The graded F-profile
(100,000 distinct users) and higher in-flight tiers belong to later Gate steps
and are NOT authorized here.
"""

from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.query_load.accounting import RunAccounting  # noqa: E402
from tools.query_load.client import QueryClient  # noqa: E402
from tools.query_load.d0_fixture import build_d0_runs  # noqa: E402
from tools.query_load.driver import (  # noqa: E402
    BEARER_AUDIENCE,
    BEARER_ISSUER,
    build_application,
    run_concurrent,
    run_paging,
    run_serial,
    start_server,
    verify_all,
)
from g15.query.auth import issue_token  # noqa: E402


def _require_opt_in() -> None:
    if os.environ.get("G15_RUN_QUERY_LOAD") != "1":
        pytest.skip("opt-in: set G15_RUN_QUERY_LOAD=1 to run the query load test")


def _machine_facts() -> dict[str, str]:
    return {
        "os": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": str(os.cpu_count()),
    }


def _latency_distribution(records) -> str:
    if not records:
        return "none"
    latencies = sorted(r.completed_monotonic - r.started_monotonic for r in records)
    count = len(latencies)
    return (
        f"n={count} min={latencies[0]:.4f}s "
        f"p50={latencies[count // 2]:.4f}s "
        f"p95={latencies[int(count * 0.95)]:.4f}s "
        f"max={latencies[-1]:.4f}s"
    )


def test_gate0_low_load_query_measurement(tmp_path: Path) -> None:
    _require_opt_in()
    runs_root = tmp_path / "runs"
    db_paths = build_d0_runs(runs_root)
    import secrets

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
        started = time.perf_counter()
        serial = run_serial(client, 10, db_paths)
        serial_seconds = time.perf_counter() - started

        started = time.perf_counter()
        concurrent = run_concurrent(client, 10, db_paths)
        concurrent_seconds = time.perf_counter() - started

        started = time.perf_counter()
        paging = run_paging(client, 0, db_paths)
        paging_seconds = time.perf_counter() - started
    finally:
        server.shutdown()
        server.server_close()

    all_results = serial + concurrent + paging
    assert all((status == 200 for status, _body, _run in all_results))
    assert verify_all(all_results, db_paths) == []
    assert accounting.conservation_report() == []

    print("machine:", _machine_facts())
    print(f"serial_seconds={serial_seconds:.4f}")
    print(f"concurrent_seconds={concurrent_seconds:.4f}")
    print(f"paging_seconds={paging_seconds:.4f}")
    print("peak_client_in_flight=", accounting.peak_client_in_flight)
    print("peak_server_in_flight=", accounting.peak_server_in_flight)
    print("client_latency:", _latency_distribution(accounting.client_records))
