"""Two-process Gate 0 workload with real HTTP, authentication and SQLite.

A concurrent wave pre-establishes its real TCP connections, then sends one
page_size=100 request per distinct authenticated user. Connection preparation
and the client-only launch barrier are recorded separately from request latency.
No handler delay, server barrier, load throttling or automatic retry is used.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import multiprocessing
import secrets
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from http.client import HTTPConnection, IncompleteRead
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from g15.query.auth import issue_token

from .accounting import ClientRecord, RunAccounting, _latencies
from .client import QueryClient, _finite_float, _invalid_constant
from .d0_fixture import (
    D0_ACTOR_REF,
    D0_DEVICE_COUNT,
    D0_SUBJECT,
    build_d0_runs,
    device_id,
    device_run_id,
)
from .driver import BEARER_AUDIENCE, BEARER_ISSUER
from .evidence import query_plans, source_facts, write_evidence
from .paging import PagingWalk
from .process_server import EndpointSampler, serve_endpoint
from .runner import _failure, _new_report, _raw_accounting, validate_run

PAGE_SIZE = 100


def _send_preconnected(
    base_url: str,
    token: str,
    actor: str,
    index: int,
    barrier: threading.Barrier,
    accounting: RunAccounting,
) -> tuple[tuple[int, dict[str, object], str], dict[str, Any]]:
    """Establish TCP before launch; measure latency only at actual send."""
    location = urlsplit(base_url)
    connection = HTTPConnection(location.hostname, location.port, timeout=5)
    run_id = device_run_id(index % D0_DEVICE_COUNT)
    device = device_id(index % D0_DEVICE_COUNT)
    preparation: dict[str, Any] = {
        "user_id": actor,
        "connect_started_monotonic": time.perf_counter(),
        "connected_monotonic": None,
        "barrier_released_monotonic": None,
        "request_send_started_monotonic": None,
        "request_sent": False,
        "error": "",
    }
    status, raw, body, error = 0, b"", {}, ""
    started = None
    waiting = None
    try:
        connection.connect()
        preparation["connected_monotonic"] = time.perf_counter()
        barrier.wait(timeout=10)
        preparation["barrier_released_monotonic"] = time.perf_counter()
        started = accounting.client_start()
        preparation["request_send_started_monotonic"] = started
        connection.request(
            "GET",
            f"/v1/runs/{run_id}/devices/{device}/health?page_size={PAGE_SIZE}",
            headers={"Authorization": f"Bearer {token}", "Connection": "close"},
        )
        preparation["request_sent"] = True
        waiting = time.perf_counter()
        response = connection.getresponse()
        status = response.status
        raw = response.read()
        decoded = json.loads(
            raw.decode("utf-8"),
            parse_float=_finite_float,
            parse_constant=_invalid_constant,
        )
        if isinstance(decoded, dict):
            body = decoded
        else:
            error = "non_object_json"
        if not error and status != 200:
            error = f"http_status_{status}"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Preserve all attempted calls, without secrets/URL/exception messages.
        error = type(exc).__name__
        preparation["error"] = error
        if isinstance(exc, IncompleteRead):
            raw = exc.partial
        if started is None:
            barrier.abort()
    finally:
        # Pre-send failures are explicit attempts, never successful request latency.
        if started is None:
            started = accounting.client_start()
        if waiting is None:
            waiting = time.perf_counter()
        accounting.client_complete(
            ClientRecord(
                user_id=actor,
                run_id=run_id,
                device_id=device,
                started_monotonic=started,
                waiting_monotonic=waiting,
                completed_monotonic=time.perf_counter(),
                status=status,
                server_request_id=str(body.get("request_id", "")),
                trace_id=str(body.get("trace_id", "")),
                error=error,
                response_bytes_base64=base64.b64encode(raw).decode("ascii"),
            )
        )
        connection.close()
    return (status, body, run_id), preparation


def _wave(
    base_url: str, identities: list[tuple[str, str]], accounting: RunAccounting
) -> tuple[list, list]:
    barrier = threading.Barrier(len(identities))
    responses, preparation = [], []
    with ThreadPoolExecutor(max_workers=len(identities)) as pool:
        futures = [
            pool.submit(
                _send_preconnected,
                base_url,
                token,
                actor,
                index,
                barrier,
                accounting,
            )
            for index, (token, actor) in enumerate(identities)
        ]
        for future in as_completed(futures):
            response, facts = future.result()
            responses.append(response)
            preparation.append(facts)
    return responses, preparation


def _identities(key: bytes, count: int) -> list[tuple[str, str]]:
    return [
        (
            issue_token(
                key=key,
                issuer=BEARER_ISSUER,
                audience=BEARER_AUDIENCE,
                subject=D0_SUBJECT if i == 0 else f"{D0_SUBJECT}-{i:03d}",
                now=1_900,
            ),
            D0_ACTOR_REF if i == 0 else f"{D0_ACTOR_REF}-{i:03d}",
        )
        for i in range(count)
    ]


class EndpointControlFailure(RuntimeError):
    """Preserve the child's already collected raw evidence on control failure."""

    def __init__(self, message: dict[str, Any]) -> None:
        super().__init__("unexpected endpoint control response")
        self.details = message


def _receive(control: Any, expected_kind: str) -> dict[str, Any]:
    if not control.poll(20):
        raise TimeoutError("endpoint control timed out")
    message = control.recv()
    if message.get("kind") != expected_kind:
        # Do not turn an unexpected/failed child response into success.
        raise EndpointControlFailure(message)
    return message


def _validate_resources(report: dict[str, Any]) -> list[str]:
    """Reconcile endpoint identity and every summary against origin raw samples."""
    resources = report.get("endpoint_resources") or {}
    client, server = resources.get("client") or {}, resources.get("server") or {}
    problems = []

    def number(value: Any) -> bool:
        return type(value) in (int, float) and math.isfinite(value)

    def equal(actual: Any, expected: Any) -> bool:
        return (
            number(actual)
            and number(expected)
            and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
        )

    if not client.get("process_id") or not server.get("process_id"):
        problems.append("endpoint process identity missing")
    elif client["process_id"] == server["process_id"]:
        problems.append("client and server share a process")
    if client.get("process_id") != report.get("client_process_id"):
        problems.append("client resource PID differs from controller origin PID")
    if server.get("process_id") != report.get("server_ready_process_id"):
        problems.append("server resource PID differs from ready origin PID")
    transport_pids = {
        row.get("process_id") for row in report.get("transport_events", [])
    }
    if transport_pids != {server.get("process_id")}:
        problems.append("server resource PID differs from transport origin PIDs")

    for name, value in (("client", client), ("server", server)):
        samples = value.get("samples") or []
        if len(samples) < 2:
            problems.append(f"{name} lacks start/end resource observations")
            continue
        if value.get("endpoint") != name or type(value.get("process_id")) is not int:
            problems.append(f"{name} resource identity invalid")
        if value.get("errors"):
            problems.append(f"{name} resource sampling failed")
        for sample in samples:
            if (
                sample.get("endpoint") != name
                or sample.get("process_id") != value.get("process_id")
                or sample.get("clock") != "perf_counter"
                or type(sample.get("monotonic_ns")) is not int
                or sample.get("monotonic_ns", -1) <= 0
                or sample.get("monotonic_seconds")
                != sample.get("monotonic_ns", 0) / 1_000_000_000
            ):
                problems.append(f"{name} sample origin identity/clock mismatch")
            for key in ("monotonic_seconds", "cpu_user_seconds", "cpu_system_seconds"):
                if not number(sample.get(key)) or sample[key] < 0:
                    problems.append(f"{name} sample {key} invalid")
            for key in ("rss_bytes", "open_fds", "open_handles"):
                if sample.get(key) is not None and (
                    type(sample[key]) is not int or sample[key] < 0
                ):
                    problems.append(f"{name} sample {key} invalid")
        valid_numbers = all(
            number(row.get(key))
            for row in samples
            for key in ("monotonic_seconds", "cpu_user_seconds", "cpu_system_seconds")
        )
        if not valid_numbers:
            continue
        for previous, current in zip(samples, samples[1:]):
            if current["monotonic_seconds"] <= previous["monotonic_seconds"]:
                problems.append(f"{name} sample clock did not advance")
            for key in ("cpu_user_seconds", "cpu_system_seconds"):
                if current[key] < previous[key]:
                    problems.append(f"{name} cumulative CPU decreased")
        first, last = samples[0], samples[-1]
        elapsed = last["monotonic_seconds"] - first["monotonic_seconds"]
        user = last["cpu_user_seconds"] - first["cpu_user_seconds"]
        system = last["cpu_system_seconds"] - first["cpu_system_seconds"]
        expected = {
            "elapsed_seconds": elapsed,
            "cpu_user_delta_seconds": user,
            "cpu_system_delta_seconds": system,
            "cpu_total_delta_seconds": user + system,
        }
        if elapsed <= 0:
            problems.append(f"{name} resource interval invalid")
        for key, actual in expected.items():
            if not equal(value.get(key), actual):
                problems.append(f"{name} {key} differs from raw samples")
        maxima = value.get("sampled_maxima") or {}
        for key in ("rss_bytes", "open_fds", "open_handles"):
            observed = [row[key] for row in samples if row.get(key) is not None]
            # Malformed raw values have already failed above.
            if any(type(item) is not int for item in observed):
                continue
            expected_maximum = max(observed) if observed else None
            if maxima.get(key) != expected_maximum:
                problems.append(f"{name} {key} maximum differs from raw samples")
        if maxima.get("rss_bytes") is None:
            problems.append(f"{name} RSS observations unavailable")
        if all(maxima.get(key) is None for key in ("open_fds", "open_handles")):
            problems.append(f"{name} fd/handle observations unavailable")
        first_io, last_io = first.get("io_counters"), last.get("io_counters")
        expected_io = (
            {key: last_io[key] - first_io[key] for key in first_io if key in last_io}
            if isinstance(first_io, dict) and isinstance(last_io, dict)
            else None
        )
        if value.get("process_io_delta") != expected_io:
            problems.append(f"{name} I/O delta differs from raw samples")
        if expected_io is not None and any(delta < 0 for delta in expected_io.values()):
            problems.append(f"{name} cumulative I/O decreased")
        cpus = value.get("logical_cpu_count")
        if type(cpus) is not int or cpus <= 0:
            problems.append(f"{name} logical CPU count invalid")
        elif elapsed > 0:
            if not equal(
                value.get("cpu_percent_one_core"), 100 * (user + system) / elapsed
            ):
                problems.append(
                    f"{name} one-core CPU percentage differs from raw samples"
                )
            if not equal(
                value.get("cpu_percent_host_capacity"),
                100 * (user + system) / elapsed / cpus,
            ):
                problems.append(f"{name} host CPU percentage differs from raw samples")
    return problems


def run_process_measurement(
    runs_root: Path, *, concurrent: int = 10, serial: int = 10, workers: int = 64
) -> dict[str, Any]:
    """Keep all responses/failures while measuring distinct endpoint processes."""
    if (
        concurrent not in (10, 50)
        or not 1 <= serial <= 10
        or not concurrent <= workers <= 64
    ):
        raise ValueError("requires serial 1..10, concurrent 10/50, and N..64 workers")
    report = _new_report(serial, concurrent, workers)
    report["format"] = "g15-query-gate0-two-process-v1"
    report["command"][2] = "tools.query_load.process_runner"
    report["workload"].update(
        page_size=PAGE_SIZE,
        connection_mode="preconnected short HTTP connections; one request each",
        arrival_mode="client-only barrier after TCP connect, before request send",
        latency_basis="phase-specific; mixed statistics include different connection setup scopes",
        response_windows_per_request=100,
        no_automatic_retry=True,
    )
    accounting = RunAccounting()
    report["endpoint_resources"] = {"client": None, "server": None}
    report["client_process_id"] = os.getpid()
    report["connection_preparation"] = []
    events, transport_events, serial_results, concurrent_results = [], [], [], []
    paging, db_paths = PagingWalk(), []
    process = None
    control = None
    stream_reader = None
    event_receiver = None
    event_sender = None
    child_control = None
    streamed = {"transport": [], "application": [], "resource": [], "errors": []}

    def receive_events() -> None:
        try:
            while True:
                message = event_receiver.recv()
                if message.get("kind") in (
                    "transport",
                    "application",
                    "resource",
                ) and isinstance(message.get("event"), dict):
                    streamed[message["kind"]].append(message["event"])
                else:
                    streamed["errors"].append("invalid streamed event")
        except EOFError:
            pass
        except Exception as exc:
            streamed["errors"].append(type(exc).__name__)

    sampler = None
    finish_collected = False
    source_start = None
    try:
        source_start = source_facts()
        report["source"] = source_start
        db_paths = build_d0_runs(runs_root)
        bearer_key, cursor_key = secrets.token_bytes(32), secrets.token_bytes(32)
        context = multiprocessing.get_context("spawn")
        control, child_control = context.Pipe()
        event_receiver, event_sender = context.Pipe(duplex=False)
        stream_reader = threading.Thread(target=receive_events, daemon=True)
        stream_reader.start()
        process = context.Process(
            target=serve_endpoint,
            args=(
                child_control,
                event_sender,
                str(runs_root),
                bearer_key,
                cursor_key,
                concurrent,
                workers,
            ),
            name="g15-real-http-server",
        )
        process.start()
        child_control.close()
        event_sender.close()
        ready = _receive(control, "ready")
        report["server_ready_process_id"] = ready["process_id"]
        identities = _identities(bearer_key, concurrent)
        client = QueryClient(
            ready["base_url"], identities[0][0], identities[0][1], accounting
        )
        control.send({"kind": "begin"})
        _receive(control, "begun")
        sampler = EndpointSampler("client")
        sampler.start()
        for index in range(serial):
            run_id = device_run_id(index % D0_DEVICE_COUNT)
            status, body = client.query(
                run_id, device_id(index % D0_DEVICE_COUNT), page_size=PAGE_SIZE
            )
            serial_results.append((status, body, run_id))
        concurrent_results, preparation = _wave(
            ready["base_url"], identities, accounting
        )
        report["connection_preparation"] = preparation
        # Explicitly use the shared traversal implementation, including raw oracle.
        from .paging import run_paging

        paging = run_paging(client, 0, db_paths)
        report["endpoint_resources"]["client"] = sampler.stop()
        sampler = None
        control.send(
            {"kind": "finish", "expected_completed": len(accounting.client_records)}
        )
        finished = _receive(control, "finished")
        finish_collected = True
        transport_events = finished["transport_events"]
        events = finished["application_events"]
        report["endpoint_resources"]["server"] = finished["resources"]
        report["server_drain_problems"] = finished["drain_problems"]
        report["server_stream_errors"] = finished.get("stream_errors", [])
        report["query_plans"] = query_plans(db_paths, page_size=PAGE_SIZE)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _failure(report, "two_process_run", exc)
        if isinstance(exc, EndpointControlFailure):
            transport_events = exc.details.get("transport_events", [])
            events = exc.details.get("application_events", [])
            report["endpoint_resources"]["server"] = exc.details.get("resources")
            report["child_failure"] = {
                "error": exc.details.get("error", "unexpected_control")
            }
    finally:
        if sampler is not None:
            report["endpoint_resources"]["client"] = sampler.stop()
        if control is not None and process is not None and process.is_alive():
            if not finish_collected:
                try:
                    control.send(
                        {
                            "kind": "finish",
                            "expected_completed": len(accounting.client_records),
                        }
                    )
                    finished = _receive(control, "finished")
                    transport_events = finished["transport_events"]
                    events = finished["application_events"]
                    report["endpoint_resources"]["server"] = finished["resources"]
                    report["server_drain_problems"] = finished["drain_problems"]
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    _failure(report, "endpoint_failure_snapshot", exc)
            try:
                control.send({"kind": "stop"})
            except (OSError, EOFError):
                pass
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                _failure(report, "endpoint_stop_timeout", TimeoutError())
        if process is not None:
            report["server_process_exit_code"] = process.exitcode
            if process.exitcode != 0:
                _failure(report, "endpoint_exit", RuntimeError())
        if control is not None:
            control.close()
        # Also close parent duplicates when spawn itself fails, so the reader
        # reaches EOF instead of leaving a blocked daemon/handle behind.
        if event_sender is not None:
            event_sender.close()
        if child_control is not None:
            child_control.close()
        if stream_reader is not None:
            stream_reader.join(timeout=5)
            if stream_reader.is_alive():
                streamed["errors"].append("event reader did not reach EOF")
        if event_receiver is not None:
            event_receiver.close()
        report["streamed_evidence"] = streamed
        stream_problems = list(streamed["errors"]) + report.get(
            "server_stream_errors", []
        )
        if finish_collected:
            for name, snapshot in (
                ("transport", transport_events),
                ("application", events),
            ):
                encode = lambda rows: Counter(
                    json.dumps(row, sort_keys=True, allow_nan=False) for row in rows
                )
                if encode(snapshot) != encode(streamed[name]):
                    stream_problems.append(f"{name} stream/snapshot mismatch")
            resource_snapshot_rows = (report["endpoint_resources"]["server"] or {}).get(
                "samples", []
            )
            if encode(resource_snapshot_rows) != encode(streamed["resource"]):
                stream_problems.append("resource stream/snapshot mismatch")
        # The stream is authoritative: even abrupt child exit retains delivered
        # origin events rather than replacing failure evidence with empty lists.
        transport_events = streamed["transport"]
        events = streamed["application"]
        # Replay origin-stamped events, not receipt-time approximations.
        for event in transport_events:
            accounting.server_event(event)
        report["transport_events"] = transport_events
        report["application_events"] = events
        report["serial_responses"] = serial_results
        report["concurrent_responses"] = concurrent_results
        report["accounting"] = _raw_accounting(accounting)
        report["paging"] = {
            "raw_responses": paging.raw,
            "consumed_responses": paging.consumed,
            "discards": paging.discards,
            "incomplete": True,
        }
        try:
            report["accounting"] = accounting.evidence()
            report["paging"] = paging.evidence()
            report.update(
                validate_run(
                    serial_results + concurrent_results,
                    paging,
                    db_paths,
                    accounting,
                    events,
                    page_size=PAGE_SIZE,
                )
            )
            report["checks"]["endpoint_resources"] = _validate_resources(report)
            report["checks"]["streamed_evidence"] = stream_problems
            report["checks"]["server_drain"] = report.get(
                "server_drain_problems", ["endpoint drain not collected"]
            )
            report["source_after"] = source_facts()
            unchanged = source_start is not None and (
                source_start.get("git_sha") == report["source_after"].get("git_sha")
                and source_start.get("content_sha256")
                == report["source_after"].get("content_sha256")
            )
            report["checks"]["fixed_source"] = (
                [] if unchanged else ["source changed during run"]
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _failure(report, "two_process_validation", exc)
    report["quality_passed"] = (
        not report.get("failures")
        and bool(report.get("checks"))
        and not any(report["checks"].values())
    )
    try:
        client_records = accounting.client_records
        phase_ids = {
            "serial": {
                body.get("request_id")
                for _, body, _ in serial_results
                if body.get("request_id")
            },
            "concurrent": {
                body.get("request_id")
                for _, body, _ in concurrent_results
                if body.get("request_id")
            },
            "paging": {
                body.get("request_id")
                for _, body, _ in paging.raw
                if body.get("request_id")
            },
        }
        bases = {
            "serial": "QueryClient call start to complete response read and decode; includes TCP connect and dispatch",
            "concurrent": "actual send start after TCP preconnect/client barrier to complete response read and decode",
            "paging": "QueryClient call start to complete response read and decode; includes TCP connect and dispatch",
        }
        report["latency_by_phase"] = {}
        report["accounting_by_phase"] = {}
        classified_ids = set().union(*phase_ids.values())
        for phase, request_ids in phase_ids.items():
            phase_clients = [
                row for row in client_records if row.server_request_id in request_ids
            ]
            phase_servers = [
                row
                for row in accounting.server_records
                if row.request_id in request_ids
            ]
            phase_accounting = RunAccounting(
                client_records=phase_clients,
                server_records=phase_servers,
                _client_started=len(phase_clients),
            ).evidence()
            report["accounting_by_phase"][phase] = phase_accounting
            report["latency_by_phase"][phase] = {
                **phase_accounting["latency_seconds"],
                "basis": bases[phase],
            }
        wave = report["accounting_by_phase"]["concurrent"]
        wave_peaks = wave.get("peaks") or {}
        report["target_reached"] = (
            wave.get("trusted") is True
            and len(phase_ids["concurrent"]) == concurrent
            and len(wave.get("client_records", [])) == concurrent
            and all(
                (wave_peaks.get(side, {}).get("in_flight") or 0) >= concurrent
                and (wave_peaks.get(side, {}).get("distinct_users_at_peak") or 0)
                >= concurrent
                for side in ("client", "server")
            )
        )
        report["target_basis"] = (
            "only the actual concurrent response request_id set; serial and paging intervals excluded"
        )
        report["latency_by_phase"]["unclassified"] = {
            **_latencies(
                [
                    row
                    for row in client_records
                    if row.server_request_id not in classified_ids
                ]
            ),
            "basis": "failure records not uniquely associated with a saved phase response",
        }
        if report.get("accounting", {}).get("latency_seconds") is not None:
            report["accounting"]["latency_seconds"][
                "scope"
            ] = "mixed serial/concurrent/paging; connection preparation scopes differ; see latency_by_phase"
        elapsed = (
            max(row.completed_monotonic for row in client_records)
            - min(row.started_monotonic for row in client_records)
            if client_records
            else None
        )
        successful = sum(row.status == 200 and not row.error for row in client_records)
        report["throughput"] = {
            "scope": "serial, concurrent and paging calls including inter-call client work",
            "elapsed_seconds": elapsed,
            "attempts_completed": len(client_records),
            "responses_received": sum(row.status > 0 for row in client_records),
            "successful_responses": successful,
            "successful_responses_per_second": (
                successful / elapsed if elapsed and elapsed > 0 else None
            ),
            "response_bytes_received": sum(
                len(base64.b64decode(row.response_bytes_base64))
                for row in client_records
            ),
            "errors": sum(bool(row.error) for row in client_records),
        }
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Diagnostics must never replace the already retained raw run with an
        # empty CLI fallback report.
        _failure(report, "phase_diagnostics", exc)
        report["target_reached"] = False
        report["phase_diagnostics_incomplete"] = True
    report["remaining_gates"] = [
        "independent review",
        "two Linux evidence",
        "application queue absent; queued N/A requires reviewed scope ruling",
        "per-endpoint network wire bytes unmeasured",
        "raw exhausted-stream replay protocol limitation",
    ]
    report["gate0_passed"] = False
    report["exit_code"] = int(
        not (report["quality_passed"] and report["target_reached"])
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """Write a fresh report for success or failure, never overwriting prior runs."""
    parser = argparse.ArgumentParser(description="D0 two-process HTTP measurement")
    parser.add_argument("--serial", type=int, default=10)
    parser.add_argument("--concurrent", type=int, choices=(10, 50), default=10)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("--output already exists; retain every previous attempt")
    report = _new_report(args.serial, args.concurrent, args.workers)
    try:
        with tempfile.TemporaryDirectory(prefix="g15-process-gate0-") as directory:
            report = run_process_measurement(
                Path(directory) / "runs",
                serial=args.serial,
                concurrent=args.concurrent,
                workers=args.workers,
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _failure(report, "workspace", exc)
    try:
        storage_parent = args.output.parent.resolve()
        while not storage_parent.exists() and storage_parent != storage_parent.parent:
            storage_parent = storage_parent.parent
        usage = shutil.disk_usage(storage_parent)
        report["evidence_volume_before_write"] = {
            "scope": "filesystem containing requested evidence directory",
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    except OSError:
        report["evidence_volume_before_write"] = None
    write_evidence(args.output, report)
    for name in ("quality_passed", "target_reached", "gate0_passed", "exit_code"):
        print(name, report[name])
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
