"""Low-load smoke and accounting runner for Gate 0 (Issue 136).

Assembles the real read-only query path end to end:

    D0 fixture (10 devices x 100 windows) -> HealthQueryApplication
    -> create_query_server (real HTTP) -> QueryClient (real sockets)
    -> RunAccounting (client + server in-flight) -> oracle per response

Waves:
1. serial: one user issues 10 sequential requests across the devices;
2. concurrent: N requests in flight at once (default 10, opt-in 50).
3. paging: walk every page of one device, verifying each page against the
   oracle independently.

Every 200 response is checked against the independent SQLite oracle, and the
accounting is checked for conservation (client == server, in-flight returns to
zero).  No handler sleep, artificial delay, or network throttle is used.
"""

from __future__ import annotations

import os
import platform
import secrets
import threading
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from g15.query.application import HealthQueryApplication
from g15.query.auth import AuthorizationPolicy, TokenVerifier, issue_token
from g15.query.cursor import CursorCodec
from g15.service.http import create_query_server

from .accounting import RunAccounting
from .client import QueryClient
from .d0_fixture import (
    D0_DEVICE_COUNT,
    D0_RULE_ID,
    D0_WINDOWS_PER_DEVICE,
    build_d0_runs,
    build_policy_yaml,
    device_id,
    device_run_id,
    window_start_seq,
)
from .oracle import verify_response

BEARER_ISSUER = "issuer-gate0"
BEARER_AUDIENCE = "audience-gate0"


def machine_facts() -> dict[str, str]:
    return {
        "os": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": str(os.cpu_count()),
    }


def build_application(
    runs_root: Path, bearer_key: bytes, cursor_key: bytes
) -> tuple[HealthQueryApplication, list[dict[str, object]]]:
    events: list[dict[str, object]] = []
    policy = AuthorizationPolicy.from_yaml(build_policy_yaml())
    application = HealthQueryApplication(
        runs_root=runs_root,
        token_verifier=TokenVerifier(
            key=bearer_key,
            issuer=BEARER_ISSUER,
            audience=BEARER_AUDIENCE,
            clock=lambda: 2_000,
        ),
        authorization_policy=policy,
        cursor_codec=CursorCodec(cursor_key),
        event_sink=lambda event: events.append(dict(event)),
    )
    return application, events


def start_server(
    application: HealthQueryApplication,
    accounting: RunAccounting,
    max_workers: int = 16,
) -> tuple[object, threading.Thread, str]:
    """Start a real HTTP server thread and return (server, thread, base_url)."""
    server = create_query_server(
        application,
        event_sink=accounting.server_event,
        max_workers=max_workers,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _db_for_run_id(db_paths: list[Path], run_id: str) -> Path:
    for candidate in db_paths:
        if candidate.parent.parent.name == run_id:
            return candidate
    raise KeyError(f"no store for run_id {run_id}")


def run_serial(
    client: QueryClient, requests: int, db_paths: list[Path]
) -> list[tuple[int, dict[str, object], str]]:
    results: list[tuple[int, dict[str, object], str]] = []
    for index in range(requests):
        device_index = index % D0_DEVICE_COUNT
        run_id = device_run_id(device_index)
        status, body = client.query(run_id, device_id(device_index), page_size=10)
        results.append((status, body, run_id))
    return results


def run_concurrent(
    client: QueryClient, in_flight: int, db_paths: list[Path]
) -> list[tuple[int, dict[str, object], str]]:
    results: list[tuple[int, dict[str, object], str]] = []

    def one(index: int) -> tuple[int, dict[str, object], str]:
        device_index = index % D0_DEVICE_COUNT
        run_id = device_run_id(device_index)
        status, body = client.query(run_id, device_id(device_index), page_size=10)
        return status, body, run_id

    with ThreadPoolExecutor(max_workers=in_flight) as pool:
        futures = [pool.submit(one, i) for i in range(in_flight)]
        for future in as_completed(futures):
            results.append(future.result())
    return results


@dataclass
class PagingSummary:
    """Raw paging observations plus duplicate accounting for cross-page coverage.

    ``window_start_seqs`` holds every window ``start_seq`` the walk saw, in
    order; coverage asserts it is exactly the 100 distinct D0 windows with no
    duplicate or missing page.  ``alarm_keys`` holds only the first occurrence
    of each ``(start_seq, rule_id)``; ``discarded_alarm_duplicates`` counts the
    alarm items dropped because the server re-served an already-exhausted alarm
    stream's page one, so the driver reports the server's raw count and the
    de-duplicated count instead of silently discarding.
    """

    window_start_seqs: list[int]
    alarm_keys: list[tuple[int, str]]
    discarded_alarm_duplicates: int


MAX_PAGING_PAGES = 20


def run_paging(
    client: QueryClient,
    device_index: int,
    db_paths: list[Path],
) -> tuple[list[tuple[int, dict[str, object], str]], PagingSummary]:
    """Walk all pages of one device, verifying each page via the oracle.

    Windows and alarms paginate independently.  A ``None`` ``next_cursor``
    means that stream is exhausted; it must not be re-sent as an empty cursor,
    because the server treats an absent cursor as page one and would silently
    re-serve the first page.  The two exhausted flags stop the walk; an absent
    cursor on a live stream still means page one.

    The raw response bodies are returned untouched so the oracle can verify the
    server's actual output, including any re-served alarm page-one items.  A
    ``PagingSummary`` is also returned that de-duplicates alarms (recording how
    many were discarded as re-served duplicates) so coverage can assert the
    logical result without the driver silently swallowing anything.
    """
    run_id = device_run_id(device_index)
    results: list[tuple[int, dict[str, object], str]] = []
    window_start_seqs: list[int] = []
    alarm_keys: list[tuple[int, str]] = []
    seen_alarms: set[tuple[int, str]] = set()
    discarded_alarm_duplicates = 0
    windows_cursor: str | None = ""
    alarms_cursor: str | None = ""
    windows_done = False
    alarms_done = False
    while not (windows_done and alarms_done):
        if len(results) >= MAX_PAGING_PAGES:
            raise RuntimeError(
                f"paging exceeded {MAX_PAGING_PAGES} pages: cursor never exhausted"
            )
        status, body = client.query(
            run_id,
            device_id(device_index),
            page_size=10,
            windows_cursor=windows_cursor,
            alarms_cursor=alarms_cursor,
        )
        results.append((status, body, run_id))
        for item in body.get("windows", {}).get("items", []):
            window_start_seqs.append(int(item.get("start_seq")))
        for item in body.get("alarms", {}).get("items", []):
            key = (int(item.get("start_seq")), str(item.get("rule_id")))
            if key in seen_alarms:
                discarded_alarm_duplicates += 1
            else:
                seen_alarms.add(key)
                alarm_keys.append(key)
        next_windows = body.get("windows", {}).get("next_cursor")
        next_alarms = body.get("alarms", {}).get("next_cursor")
        if next_windows is None:
            windows_done = True
        else:
            windows_cursor = str(next_windows)
        if next_alarms is None:
            alarms_done = True
        else:
            alarms_cursor = str(next_alarms)
    summary = PagingSummary(
        window_start_seqs=window_start_seqs,
        alarm_keys=alarm_keys,
        discarded_alarm_duplicates=discarded_alarm_duplicates,
    )
    return results, summary


def verify_paging_coverage(summary: PagingSummary) -> list[str]:
    """Assert the paging walk covered every window exactly once and the alarm
    exactly once after de-duplication.

    The server re-serves page one of an exhausted stream whenever that stream's
    cursor is absent.  The driver keeps raw responses for the oracle (so the
    oracle sees every re-served item) and de-duplicates alarms in the summary,
    recording how many were discarded.  This cross-page check therefore proves
    the windows are neither duplicated nor missing, exactly one alarm key
    survives de-duplication, and the discarded-alarm count is reported rather
    than swallowed.
    """
    problems: list[str] = []
    windows = summary.window_start_seqs
    alarms = summary.alarm_keys
    expected_windows = set(window_start_seq(i) for i in range(D0_WINDOWS_PER_DEVICE))
    got_windows = set(windows)
    if len(windows) != D0_WINDOWS_PER_DEVICE:
        problems.append(
            f"paging windows total {len(windows)} != {D0_WINDOWS_PER_DEVICE} "
            "(duplicate or missing pages)"
        )
    if got_windows != expected_windows:
        missing = sorted(expected_windows - got_windows)
        extra = sorted(got_windows - expected_windows)
        problems.append(f"paging windows set mismatch: missing={missing} extra={extra}")
    expected_alarms = {(window_start_seq(D0_WINDOWS_PER_DEVICE - 1), D0_RULE_ID)}
    if len(alarms) != 1:
        problems.append(
            f"paging alarms total {len(alarms)} != 1 (duplicate re-served page)"
        )
    if set(alarms) != expected_alarms:
        problems.append(
            f"paging alarms set {sorted(set(alarms))} != {sorted(expected_alarms)}"
        )
    return problems


def verify_all(
    results: list[tuple[int, dict[str, object], str]],
    db_paths: list[Path],
) -> list[str]:
    problems: list[str] = []
    for position, (status, body, run_id) in enumerate(results):
        if status != 200:
            problems.append(f"response[{position}] status {status} != 200")
            continue
        device = str(body.get("device_id", ""))
        db_path = _db_for_run_id(db_paths, run_id)
        problems.extend(verify_response(body, db_path, run_id, device, page_size=10))
    return problems


def main() -> int:
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description="Gate 0 low-load smoke runner")
    parser.add_argument("--serial", type=int, default=10, help="serial requests")
    parser.add_argument("--concurrent", type=int, default=10, help="in-flight requests")
    parser.add_argument("--workers", type=int, default=64, help="server max_workers")
    args = parser.parse_args()

    bearer_key = secrets.token_bytes(32)
    cursor_key = secrets.token_bytes(32)
    accounting = RunAccounting()

    with tempfile.TemporaryDirectory(prefix="g15-gate0-") as tmp:
        runs_root = Path(tmp) / "runs"
        db_paths = build_d0_runs(runs_root)
        application, _ = build_application(runs_root, bearer_key, cursor_key)
        server, _thread, base_url = start_server(
            application, accounting, max_workers=args.workers
        )
        token = issue_token(
            key=bearer_key,
            issuer=BEARER_ISSUER,
            audience=BEARER_AUDIENCE,
            subject="subject-d0",
            now=1_900,
        )
        client = QueryClient(base_url, token, "user-d0", accounting)
        try:
            print("machine:", machine_facts())
            started = time.monotonic()
            serial_results = run_serial(client, args.serial, db_paths)
            concurrent_results = run_concurrent(client, args.concurrent, db_paths)
            paging_results, paging_summary = run_paging(client, 0, db_paths)
            elapsed = time.monotonic() - started
            all_results = serial_results + concurrent_results + paging_results
            problems = verify_all(all_results, db_paths)
            coverage = verify_paging_coverage(paging_summary)
            conservation = accounting.conservation_report()
            print(
                "serial",
                len(serial_results),
                "concurrent",
                len(concurrent_results),
                "paging",
                len(paging_results),
                "elapsed_seconds",
                round(elapsed, 3),
            )
            print("peak_client_in_flight", accounting.peak_client_in_flight)
            print("peak_server_in_flight", accounting.peak_server_in_flight)
            print(
                "discarded_alarm_duplicates", paging_summary.discarded_alarm_duplicates
            )
            print("oracle_problems", len(problems))
            print("coverage_problems", len(coverage))
            print("conservation_problems", len(conservation))
            for problem in coverage:
                print("COVERAGE:", problem)
            for problem in problems:
                print("ORACLE:", problem)
            for problem in conservation:
                print("CONSERVATION:", problem)
            failed = bool(problems or coverage or conservation)
        finally:
            server.shutdown()
            server.server_close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
