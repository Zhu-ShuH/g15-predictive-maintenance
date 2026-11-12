"""One validation and evidence path for CLI, integration and opt-in runs."""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import tempfile

from g15.query.auth import issue_token

from .accounting import RunAccounting
from .client import QueryClient
from .d0_fixture import D0_ACTOR_REF, D0_PERMISSION_DOMAIN, D0_SUBJECT, build_d0_runs
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


def _response_accounting(
    responses: list[Response], accounting: RunAccounting
) -> list[str]:
    """Bind each audited response to exactly one original client record."""
    with accounting.lock:
        records = list(accounting.client_records)
    problems = []
    response_ids = Counter(body.get("request_id") for _, body, _ in responses)
    record_ids = Counter(record.server_request_id for record in records)
    if response_ids != record_ids:
        problems.append("response/accounting request_id counts do not match")
    if any(not request_id or count != 1 for request_id, count in response_ids.items()):
        problems.append("audited responses require unique nonempty request IDs")
    by_id = {
        record.server_request_id: record
        for record in records
        if record_ids[record.server_request_id] == 1
    }
    for position, (status, body, run_id) in enumerate(responses):
        record = by_id.get(body.get("request_id"))
        if record is None:
            problems.append(f"response[{position}] has no unique client record")
            continue
        if status != record.status or run_id != record.run_id:
            problems.append(
                f"response[{position}] status/run differs from client record"
            )
        if status == 200 and (
            body.get("run_id") != record.run_id
            or body.get("device_id") != record.device_id
        ):
            problems.append(
                f"response[{position}] run/device differs from client record"
            )
        try:
            original = json.loads(
                base64.b64decode(record.response_bytes_base64, validate=True).decode(
                    "utf-8"
                )
            )
            # JSON text equality also distinguishes booleans from numbers.
            same_body = json.dumps(
                original, sort_keys=True, allow_nan=False
            ) == json.dumps(body, sort_keys=True, allow_nan=False)
        except (ValueError, UnicodeError, TypeError):
            same_body = False
        if not same_body:
            problems.append(
                f"response[{position}] body differs from recorded raw bytes"
            )
    return problems


def validate_run(
    results: list[Response],
    paging: PagingWalk,
    db_paths: list[Path],
    accounting: RunAccounting,
    events: list[dict[str, object]],
    *,
    page_size: int = 10,
    paging_page_size: int = 10,
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
    transport_by_id: dict[str, list] = {}
    for row in accounting.server_records:
        transport_by_id.setdefault(row.request_id, []).append(row)
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
        if any(row.get("permission_domain") != D0_PERMISSION_DOMAIN for row in grants):
            logs.append(
                f"{record.server_request_id}: authorized permission domain differs from D0 policy"
            )
        for row in rows:
            if ("actor_ref" in row and row["actor_ref"] != record.user_id) or (
                "permission_domain" in row
                and row["permission_domain"] != D0_PERMISSION_DOMAIN
            ):
                logs.append(
                    f"{record.server_request_id}: application identity/domain differs from grant"
                )
        for row in transport_by_id.get(record.server_request_id, []):
            needs_identity = row.stage in (
                "server_execution_completed",
                "response_completed",
            )
            if (needs_identity or row.actor_ref) and row.actor_ref != record.user_id:
                logs.append(
                    f"{record.server_request_id}: transport actor differs from grant"
                )
            if (
                needs_identity or row.permission_domain
            ) and row.permission_domain != D0_PERMISSION_DOMAIN:
                logs.append(
                    f"{record.server_request_id}: transport permission domain differs from grant"
                )
    checks = {
        "raw_oracle": verify_all(
            results, db_paths, first_page=True, page_size=page_size
        )
        + verify_all(paging.raw, db_paths, page_size=paging_page_size)
        + paging.oracle_problems,
        "consumed_coverage": verify_paging_coverage(paging.consumed),
        "traversal": paging.traversal_problems,
        "conservation": accounting.conservation_report(),
        "response_accounting": _response_accounting(results + paging.raw, accounting),
        "request_log": logs,
    }
    return {"checks": checks, "quality_passed": not any(checks.values())}


def _new_report(serial: int, concurrent: int, workers: int) -> dict[str, object]:
    """Initialize a report before any storage, source or resource collection."""
    return {
        "format": "g15-query-gate0-v2",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": None,
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
            "queued not-applicable decision requires review",
            "raw exhausted-stream replay protocol limitation",
        ],
        "resources_before": None,
        "quality_passed": False,
        "target_reached": False,
        "exit_code": 1,
    }


def _failure(report: dict[str, object], phase: str, exc: Exception) -> None:
    # Never persist exception text: it may contain private paths or credentials.
    error = type(exc).__name__
    key = "validation_failure" if phase == "validation" else "runner_failure"
    report.setdefault(key, error)
    report.setdefault("failures", []).append({"phase": phase, "error": error})
    report["quality_passed"] = False
    report["exit_code"] = 1


def _raw_accounting(accounting: RunAccounting) -> dict[str, object]:
    """Keep original records if computing accounting diagnostics fails."""
    with accounting.lock:
        return {
            "trusted": False,
            "client_records": [asdict(record) for record in accounting.client_records],
            "server_records": [asdict(record) for record in accounting.server_records],
            "peaks": None,
            "incomplete": True,
        }


def run_measurement(
    runs_root: Path, *, concurrent: int = 10, serial: int = 10, workers: int = 64
) -> dict[str, object]:
    """Run only D0 at up to 50 requests; return all evidence including failures."""
    if concurrent not in (10, 50) or not 1 <= serial <= 10 or not 1 <= workers <= 64:
        raise ValueError(
            "Gate 0 allows serial 1..10, concurrent 10/50 and workers 1..64"
        )
    report = _new_report(serial, concurrent, workers)
    accounting = RunAccounting()
    events = []
    serial_results, concurrent_results = [], []
    paging = PagingWalk()
    server = None
    db_paths = []
    try:
        report["source"] = source_facts()
        report["resources_before"] = resource_snapshot()
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
    except Exception as exc:
        _failure(report, "run", exc)
    finally:
        if server is not None:
            for phase, cleanup in (
                ("server_shutdown", server.shutdown),
                ("server_close", server.server_close),
                ("server_join", lambda: thread.join(timeout=5)),
            ):
                try:
                    cleanup()
                except Exception as exc:
                    _failure(report, phase, exc)
            if thread.is_alive():
                _failure(report, "server_join", TimeoutError())
        report["application_events"] = events
        report["serial_responses"] = serial_results
        report["concurrent_responses"] = concurrent_results
        report["accounting"] = None
        report["resources_after"] = None
        report["paging"] = {
            "raw_responses": paging.raw,
            "consumed_responses": paging.consumed,
            "discards": paging.discards,
            "raw_oracle_problems": paging.oracle_problems,
            "traversal_problems": paging.traversal_problems,
            "incomplete": True,
        }
        for name, collect in (
            ("resources_after", resource_snapshot),
            ("accounting", lambda: _raw_accounting(accounting)),
            ("accounting", accounting.evidence),
            ("paging", paging.evidence),
        ):
            try:
                report[name] = collect()
            except Exception as exc:
                _failure(report, name, exc)
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
        except Exception as exc:
            _failure(report, "validation", exc)
    if report.get("failures"):
        report["quality_passed"] = False
    peaks = (report["accounting"] or {}).get("peaks") or {}
    report["target_reached"] = bool(peaks) and all(
        (peaks.get(side, {}).get("in_flight") or 0) >= concurrent
        and (peaks.get(side, {}).get("distinct_users_at_peak") or 0) >= concurrent
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
    report = _new_report(args.serial, args.concurrent, args.workers)
    try:
        with tempfile.TemporaryDirectory(prefix="g15-gate0-") as tmp:
            report = run_measurement(
                Path(tmp) / "runs",
                concurrent=args.concurrent,
                serial=args.serial,
                workers=args.workers,
            )
    except Exception as exc:
        # Includes temporary-workspace setup/cleanup failures.  Interrupts and
        # process exits still propagate instead of being converted to a verdict.
        _failure(report, "workspace", exc)
    write_evidence(args.output, report)
    print("quality_passed", report["quality_passed"])
    print("target_reached", report["target_reached"])
    print("gate0_passed", report["gate0_passed"])
    print("exit_code", report["exit_code"])
    return int(report["exit_code"])
