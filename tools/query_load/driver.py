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
    D0_WINDOWS_PER_DEVICE,
    build_d0_runs,
    build_policy_yaml,
    device_id,
    device_run_id,
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


def run_paging(
    client: QueryClient,
    device_index: int,
    db_paths: list[Path],
) -> list[tuple[int, dict[str, object], str]]:
    """Walk all pages of one device, verifying each page via the oracle."""
    run_id = device_run_id(device_index)
    results: list[tuple[int, dict[str, object], str]] = []
    windows_cursor = ""
    alarms_cursor = ""
    while True:
        status, body = client.query(
            run_id,
            device_id(device_index),
            page_size=10,
            windows_cursor=windows_cursor,
            alarms_cursor=alarms_cursor,
        )
        results.append((status, body, run_id))
        windows = body.get("windows", {})
        alarms = body.get("alarms", {})
        next_windows = windows.get("next_cursor") or ""
        next_alarms = alarms.get("next_cursor") or ""
        if not next_windows and not next_alarms:
            break
        windows_cursor = next_windows
        alarms_cursor = next_alarms
        # Safety valve: a device has at most 100 windows / page size 10 = 10 pages.
        if len(results) > 20:
            break
    return results


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
            paging_results = run_paging(client, 0, db_paths)
            elapsed = time.monotonic() - started
            all_results = serial_results + concurrent_results + paging_results
            problems = verify_all(all_results, db_paths)
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
            print("oracle_problems", len(problems))
            print("conservation_problems", len(conservation))
            for problem in problems:
                print("ORACLE:", problem)
            for problem in conservation:
                print("CONSERVATION:", problem)
            failed = bool(problems or conservation)
        finally:
            server.shutdown()
            server.server_close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
