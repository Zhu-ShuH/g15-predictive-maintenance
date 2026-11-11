"""One validation and evidence path for CLI, integration and opt-in runs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import secrets
import sqlite3
import tempfile

from g15.query.auth import issue_token

from .accounting import RunAccounting
from .client import QueryClient
from .d0_fixture import D0_ACTOR_REF, D0_SUBJECT, build_d0_runs
from .driver import (
    BEARER_AUDIENCE,
    BEARER_ISSUER,
    build_application,
    run_concurrent,
    run_serial,
    start_server,
    verify_all,
)
from .evidence import source_facts, resource_snapshot, query_plans, write_evidence
from .paging import PagingWalk, Response, run_paging, verify_paging_coverage


def validate_run(
    results: list[Response],
    paging: PagingWalk,
    db_paths: list[Path],
    accounting: RunAccounting,
    events: list[dict[str, object]],
) -> dict[str, object]:
    """Use raw oracle, consumed coverage and three-way request reconciliation."""
    logs = []
    by_id = {}
    for event in events:
        request_id = event.get("request_id")
        if event.get("trace_id") != request_id:
            logs.append("application trace_id differs from request_id")
        by_id.setdefault(request_id, []).append(event)
    expected_ids = {r.server_request_id for r in accounting.client_records}
    if set(by_id) - expected_ids:
        logs.append("application log contains unknown request IDs")
    for record in accounting.client_records:
        rows = by_id.get(record.server_request_id, [])
        if record.status != 200:
            continue
        expected_events = [
            ("authn_checked", "started"),
            ("authn_checked", "accepted"),
            ("authz_checked", "accepted"),
            ("db_opened", "accepted"),
            ("query_started", "started"),
            ("query_completed", "success"),
        ]
        actual_events = [(row.get("stage"), row.get("result")) for row in rows]
        if actual_events != expected_events:
            logs.append(
                f"{record.server_request_id}: application stage/result sequence mismatch"
            )
        grants = [r for r in rows if r["stage"] == "authz_checked"]
        if any(row.get("actor_ref") != record.user_id for row in grants):
            logs.append(
                f"{record.server_request_id}: authenticated actor differs from client user"
            )
    checks = {
        "raw_oracle": verify_all(results, db_paths, first_page=True)
        + paging.oracle_problems,
        "consumed_coverage": verify_paging_coverage(paging.consumed),
        "traversal": paging.traversal_problems,
        "conservation": accounting.conservation_report(),
        "request_log": logs,
    }
    return {"checks": checks, "quality_passed": not any(checks.values())}


def run_measurement(
    runs_root: Path, *, concurrent: int = 10, serial: int = 10, workers: int = 64
) -> dict[str, object]:
    """Run only D0 at up to 50 requests; return all evidence including failures."""
    if concurrent not in (10, 50) or not 1 <= serial <= 10 or not 1 <= workers <= 64:
        raise ValueError(
            "Gate 0 allows serial 1..10, concurrent 10/50 and workers 1..64"
        )
    report = {
        "format": "g15-query-gate0-v2",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": source_facts(),
        "workload": {
            "devices": 10,
            "windows_per_device": 100,
            "serial": serial,
            "concurrent_submitted": concurrent,
            "distinct_authorized_users": concurrent,
            "workers": workers,
            "cache": "uncontrolled; no cold-cache claim",
        },
        "command": [
            "python",
            "-m",
            "tools.query_load.driver",
            "--serial",
            str(serial),
            "--concurrent",
            str(concurrent),
            "--workers",
            str(workers),
            "--output",
            "<new-evidence-directory>",
        ],
        "gate0_passed": False,
        "remaining_gates": [
            "independent review",
            "two Linux evidence",
            "separate client/server resources",
            "server_queued/server_executing instrumentation",
            "raw exhausted-stream replay protocol limitation",
        ],
        "resources_before": resource_snapshot(),
    }
    accounting = RunAccounting()
    events = []
    serial_results, concurrent_results = [], []
    paging = PagingWalk()
    server = None
    db_paths = []
    try:
        db_paths = build_d0_runs(runs_root)
        bearer_key, cursor_key = secrets.token_bytes(32), secrets.token_bytes(32)
        application, events = build_application(
            runs_root, bearer_key, cursor_key, users=concurrent
        )
        server, thread, base_url = start_server(
            application, accounting, max_workers=workers
        )
        clients = []
        for i in range(concurrent):
            subject = D0_SUBJECT if i == 0 else f"{D0_SUBJECT}-{i:03d}"
            actor = D0_ACTOR_REF if i == 0 else f"{D0_ACTOR_REF}-{i:03d}"
            token = issue_token(
                key=bearer_key,
                issuer=BEARER_ISSUER,
                audience=BEARER_AUDIENCE,
                subject=subject,
                now=1_900,
            )
            clients.append(QueryClient(base_url, token, actor, accounting))
        serial_results = run_serial(clients[0], serial, db_paths)
        concurrent_results = run_concurrent(clients, concurrent, db_paths)
        paging = run_paging(clients[0], 0, db_paths)
        report["query_plans"] = query_plans(db_paths)
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
        # Do not print exception text: paths or credentials may be embedded.
        report["runner_failure"] = type(exc).__name__
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        report["resources_after"] = resource_snapshot()
        report["accounting"] = accounting.evidence()
        report["application_events"] = events
        report["serial_responses"] = serial_results
        report["concurrent_responses"] = concurrent_results
        report["paging"] = paging.evidence()
        try:
            report.update(
                validate_run(
                    serial_results + concurrent_results,
                    paging,
                    db_paths,
                    accounting,
                    events,
                )
            )
        except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
            report["validation_failure"] = type(exc).__name__
            report["quality_passed"] = False
    if "runner_failure" in report:
        report["quality_passed"] = False
    peaks = report["accounting"]["peaks"]
    report["target_reached"] = all(
        peaks[side]["in_flight"] >= concurrent
        and (peaks[side]["distinct_users_at_peak"] or 0) >= concurrent
        for side in ("client", "server")
    )
    report["exit_code"] = (
        0 if report["quality_passed"] and report["target_reached"] else 1
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """Persist a fresh evidence directory on every run, including failed gates."""
    parser = argparse.ArgumentParser(description="D0 evidence and low-load measurement")
    parser.add_argument("--serial", type=int, default=10)
    parser.add_argument("--concurrent", type=int, choices=(10, 50), default=10)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument(
        "--output", type=Path, required=True, help="new evidence directory"
    )
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("--output already exists; retain every previous attempt")
    if not 1 <= args.serial <= 10 or not 1 <= args.workers <= 64:
        parser.error("Gate 0 allows serial 1..10 and workers 1..64")
    with tempfile.TemporaryDirectory(prefix="g15-gate0-") as tmp:
        report = run_measurement(
            Path(tmp) / "runs",
            concurrent=args.concurrent,
            serial=args.serial,
            workers=args.workers,
        )
    write_evidence(args.output, report)
    print("quality_passed", report["quality_passed"])
    print("target_reached", report["target_reached"])
    print("gate0_passed", report["gate0_passed"])
    print("exit_code", report["exit_code"])
    return int(report["exit_code"])
