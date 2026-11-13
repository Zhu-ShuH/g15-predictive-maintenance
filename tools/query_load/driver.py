"""Low-load smoke and accounting runner for Gate 0 (Issue 136).

Assembles the real read-only query path end to end:

    D0 fixture (10 devices x 100 windows) -> HealthQueryApplication
    -> create_query_server (real HTTP) -> QueryClient (real sockets)
    -> RunAccounting (client + server in-flight) -> oracle per response

Waves:
1. serial: one user issues 10 sequential requests across the devices;
2. concurrent: submit 10/50 requests; measure actual overlap independently.
3. paging: walk every page of one device, verifying each page against the
   oracle independently.

Every 200 response is checked against the independent SQLite oracle, and the
accounting is checked for conservation (client == server, in-flight returns to
zero).  No handler sleep, artificial delay, or network throttle is used.
"""

from __future__ import annotations

import os
import platform
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from g15.query.application import HealthQueryApplication
from g15.query.auth import AuthorizationPolicy, TokenVerifier
from g15.query.cursor import CursorCodec
from g15.service.http import create_query_server

from .accounting import RunAccounting
from .client import QueryClient
from .d0_fixture import (
    D0_DEVICE_COUNT,
    build_policy_yaml,
    device_id,
    device_run_id,
)
from .oracle import verify_response
from .paging import run_paging, verify_paging_coverage

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
    runs_root: Path,
    bearer_key: bytes,
    cursor_key: bytes,
    *,
    users: int = 1,
    event_sink: Callable[[Mapping[str, object]], None] | None = None,
) -> tuple[HealthQueryApplication, list[dict[str, object]]]:
    events: list[dict[str, object]] = []

    def collect_event(event: Mapping[str, object]) -> None:
        events.append(dict(event))
        if event_sink is not None:
            event_sink(event)

    policy = AuthorizationPolicy.from_yaml(build_policy_yaml(users))
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
        event_sink=collect_event,
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
    client: QueryClient | list[QueryClient], in_flight: int, db_paths: list[Path]
) -> list[tuple[int, dict[str, object], str]]:
    results: list[tuple[int, dict[str, object], str]] = []

    if not 1 <= in_flight <= 50:
        raise ValueError("Gate 0 allows at most 50 requests")
    barrier = threading.Barrier(in_flight)

    def one(index: int) -> tuple[int, dict[str, object], str]:
        device_index = index % D0_DEVICE_COUNT
        run_id = device_run_id(device_index)
        selected = client[index] if isinstance(client, list) else client
        barrier.wait(timeout=10)
        status, body = selected.query(run_id, device_id(device_index), page_size=10)
        return status, body, run_id

    with ThreadPoolExecutor(max_workers=in_flight) as pool:
        futures = [pool.submit(one, i) for i in range(in_flight)]
        for future in as_completed(futures):
            results.append(future.result())
    return results


def verify_all(
    results: list[tuple[int, dict[str, object], str]],
    db_paths: list[Path],
    *,
    first_page: bool = False,
    page_size: int = 10,
    request_contexts: list[dict[str, object]] | None = None,
) -> list[str]:
    problems: list[str] = []
    if request_contexts is not None and len(request_contexts) != len(results):
        problems.append("response/request context counts do not match")
    for position, (status, body, run_id) in enumerate(results):
        streams = None
        first_streams = ()
        if request_contexts is not None:
            context = (
                request_contexts[position] if position < len(request_contexts) else None
            )
            if not isinstance(context, dict) or set(context) != {
                "request_id",
                "streams",
                "page_size",
                "first_page_streams",
            }:
                problems.append(
                    f"response[{position}] missing or malformed request context"
                )
                continue
            if context["request_id"] != body.get("request_id"):
                problems.append(
                    f"response[{position}] request context identity mismatch"
                )
            streams = context["streams"]
            if streams not in (None, "both", "windows", "alarms"):
                problems.append(f"response[{position}] invalid requested streams")
                continue
            selected = (
                ("windows", "alarms") if streams in (None, "both") else (streams,)
            )
            first_streams = context["first_page_streams"]
            if (
                not isinstance(first_streams, (list, tuple))
                or any(
                    type(stream) is not str or stream not in selected
                    for stream in first_streams
                )
                or len(first_streams) != len(set(first_streams))
            ):
                problems.append(
                    f"response[{position}] invalid first-page request context"
                )
                continue
            if (
                type(context["page_size"]) is not int
                or context["page_size"] != page_size
            ):
                problems.append(
                    f"response[{position}] page_size differs from request context"
                )
        if status != 200:
            problems.append(f"response[{position}] status {status} != 200")
            continue
        device = next(
            device_id(i) for i in range(D0_DEVICE_COUNT) if device_run_id(i) == run_id
        )
        db_path = _db_for_run_id(db_paths, run_id)
        problems.extend(
            verify_response(
                body,
                db_path,
                run_id,
                device,
                page_size=page_size,
                first_page=first_page,
                first_page_streams=tuple(first_streams),
                streams=streams,
            )
        )
    return problems


def main() -> int:
    from .runner import main as run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
