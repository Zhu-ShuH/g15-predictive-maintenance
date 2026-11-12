"""Per-request accounting for the D0 HTTP harness.

The transport exposes received/completed, not queued/executing.  Counts below
measure those observed intervals only.  Server user counts are joined to client
records by the response's server-generated request ID; missing/ambiguous joins
are unknown, never silently counted as a distinct authenticated user.
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Mapping

CLIENT_STARTED = "client_started"
CLIENT_WAITING = "client_waiting"
CLIENT_COMPLETED = "client_completed"
SERVER_RECEIVED = "server_received"
SERVER_REJECTED = "rejected"
SERVER_COMPLETED = "response_completed"


@dataclass
class ClientRecord:
    user_id: str
    run_id: str
    device_id: str
    started_monotonic: float
    waiting_monotonic: float
    completed_monotonic: float
    status: int
    server_request_id: str
    trace_id: str = ""
    error: str = ""
    response_bytes_base64: str = ""


@dataclass
class ServerRecord:
    request_id: str
    stage: str
    result: str
    timestamp_utc: str
    monotonic: float
    trace_id: str = ""
    actor_ref: str = ""
    permission_domain: str = ""


def _peak(events: list[tuple[float, int, str, str | None]]) -> dict[str, object]:
    """Sweep half-open request intervals; count users at the request peak."""
    active: Counter[str] = Counter()
    users: dict[str, str | None] = {}
    result: dict[str, object] = {
        "in_flight": 0,
        "at_monotonic": None,
        "distinct_users_at_peak": 0,
        "known_distinct_users_at_peak": 0,
        "unmapped_requests_at_peak": 0,
    }
    # Completing before starting at an equal timestamp avoids invented overlap.
    for instant, delta, request_id, user_id in sorted(events):
        active[request_id] += delta
        users[request_id] = user_id
        count = sum(active.values())
        if count > result["in_flight"]:
            current = {key for key, value in active.items() if value > 0}
            known = {users[key] for key in current if users[key]}
            unknown = sum(active[key] for key in current if not users[key])
            result = {
                "in_flight": count,
                "at_monotonic": instant,
                "distinct_users_at_peak": len(known) if unknown == 0 else None,
                "known_distinct_users_at_peak": len(known),
                "unmapped_requests_at_peak": unknown,
            }
    return result


def _latencies(records: list[ClientRecord]) -> dict[str, object]:
    """Return seconds using nearest-rank percentiles, including failed calls."""
    values = sorted(
        r.completed_monotonic - r.started_monotonic
        for r in records
        if math.isfinite(r.completed_monotonic - r.started_monotonic)
        and r.completed_monotonic >= r.started_monotonic
    )
    result: dict[str, object] = {
        "count": len(values),
        "method": "nearest-rank",
        "min": None,
        "p50": None,
        "p95": None,
        "p99": None,
        "max": None,
    }
    if values:
        result.update(min=values[0], max=values[-1])
        for name, percentile in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
            result[name] = values[math.ceil(percentile * len(values)) - 1]
    return result


@dataclass
class RunAccounting:
    """Live counts and complete request records for one bounded run."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    client_records: list[ClientRecord] = field(default_factory=list)
    server_records: list[ServerRecord] = field(default_factory=list)
    _client_in_flight: int = 0
    _server_in_flight: int = 0
    peak_client_in_flight: int = 0
    peak_server_in_flight: int = 0
    _client_started: int = 0
    _counter_problems: list[str] = field(default_factory=list)

    def client_start(self) -> float:
        """Count an actual call start and return its monotonic timestamp."""
        with self.lock:
            started = time.perf_counter()
            self._client_started += 1
            self._client_in_flight += 1
            self.peak_client_in_flight = max(
                self.peak_client_in_flight, self._client_in_flight
            )
            return started

    def client_complete(self, record: ClientRecord) -> None:
        with self.lock:
            self.client_records.append(record)
            self._client_in_flight -= 1
            if self._client_in_flight < 0:
                self._counter_problems.append("client_in_flight became negative")

    def server_event(self, event: Mapping[str, object]) -> None:
        """Record only emitted transport stages, without synthesizing stages."""
        request_id = event.get("request_id", "")
        request_id = request_id if isinstance(request_id, str) else ""
        stage = event.get("stage", "")
        stage = stage if isinstance(stage, str) else ""
        with self.lock:
            if stage == SERVER_RECEIVED:
                self._server_in_flight += 1
                self.peak_server_in_flight = max(
                    self.peak_server_in_flight, self._server_in_flight
                )
            elif stage == SERVER_COMPLETED:
                self._server_in_flight -= 1
                if self._server_in_flight < 0:
                    self._counter_problems.append("server_in_flight became negative")
            self.server_records.append(
                ServerRecord(
                    request_id=request_id,
                    stage=stage,
                    result=str(event.get("result", "")),
                    timestamp_utc=str(event.get("timestamp_utc", "")),
                    monotonic=time.perf_counter(),
                    trace_id=(
                        event.get("trace_id", "")
                        if isinstance(event.get("trace_id", ""), str)
                        else ""
                    ),
                    actor_ref=str(event.get("actor_ref", "")),
                    permission_domain=str(event.get("permission_domain", "")),
                )
            )

    def conservation_report(self) -> list[str]:
        """Require one client, one received and one completed per request ID.

        Overload rejections before server_received cannot prove this invariant;
        they stay in the evidence and fail the low-load run rather than being
        made into invented server_received/completed events.
        """
        with self.lock:
            clients = list(self.client_records)
            servers = list(self.server_records)
            problems = list(self._counter_problems)
            client_in_flight = self._client_in_flight
            server_in_flight = self._server_in_flight
            client_started = self._client_started
        if client_in_flight != 0:
            problems.append(f"client_in_flight nonzero at end: {client_in_flight}")
        if server_in_flight != 0:
            problems.append(f"server_in_flight nonzero at end: {server_in_flight}")
        if client_started != len(clients):
            problems.append(
                f"client_started {client_started} != client completed {len(clients)}"
            )
        client_ids = Counter(r.server_request_id for r in clients)
        received = Counter(r.request_id for r in servers if r.stage == SERVER_RECEIVED)
        completed = Counter(
            r.request_id for r in servers if r.stage == SERVER_COMPLETED
        )
        if client_ids != received:
            problems.append("client/server received request_id counts do not match")
        if received != completed:
            problems.append("server received/completed request_id counts do not match")
        for label, counts in (
            ("client", client_ids),
            ("server_received", received),
            ("response_completed", completed),
        ):
            if "" in counts:
                problems.append(f"{label} has an empty request_id")
            for request_id, count in sorted(counts.items()):
                if count != 1:
                    problems.append(
                        f"{label} request_id {request_id!r} occurs {count} times"
                    )
        unknown_ids = {record.request_id for record in servers} - set(client_ids)
        if unknown_ids:
            problems.append(
                f"server events contain unknown request_ids: {sorted(unknown_ids)!r}"
            )
        by_id: dict[str, list[tuple[str, str]]] = {}
        for record in servers:
            by_id.setdefault(record.request_id, []).append(
                (record.stage, record.result)
            )
        for record in clients:
            if record.status == 200 and by_id.get(record.server_request_id) != [
                (SERVER_RECEIVED, "received"),
                (SERVER_COMPLETED, "success"),
            ]:
                problems.append(
                    f"{record.server_request_id}: server stage/result sequence mismatch"
                )
        active: set[str] = set()
        finished: set[str] = set()
        for record in servers:
            if record.trace_id != record.request_id or not record.trace_id:
                problems.append(f"server trace_id mismatch for {record.request_id!r}")
            if record.stage == SERVER_RECEIVED:
                if record.request_id in active or record.request_id in finished:
                    problems.append(f"server received reused {record.request_id!r}")
                active.add(record.request_id)
            elif record.stage == SERVER_COMPLETED:
                if record.request_id not in active:
                    problems.append(
                        f"server completed without received: {record.request_id!r}"
                    )
                active.discard(record.request_id)
                finished.add(record.request_id)
            elif record.stage != SERVER_REJECTED:
                problems.append(f"unexpected server stage {record.stage!r}")
        for record in clients:
            if record.trace_id != record.server_request_id or not record.trace_id:
                problems.append(
                    f"client trace_id mismatch for {record.server_request_id!r}"
                )
            times = (
                record.started_monotonic,
                record.waiting_monotonic,
                record.completed_monotonic,
            )
            if not all(math.isfinite(value) for value in times) or not (
                times[0] <= times[1] <= times[2]
            ):
                problems.append("client timestamps invalid or out of order")
            if record.error:
                problems.append(f"client failure: {record.error}")
        return problems

    def evidence(self) -> dict[str, object]:
        """Snapshot JSON-ready raw records, latency and observed interval peaks."""
        with self.lock:
            clients = list(self.client_records)
            servers = list(self.server_records)
        client_events = []
        for index, record in enumerate(clients):
            if record.completed_monotonic > record.started_monotonic:
                client_events.extend(
                    (
                        (
                            record.started_monotonic,
                            1,
                            str(index),
                            record.user_id or None,
                        ),
                        (
                            record.completed_monotonic,
                            -1,
                            str(index),
                            record.user_id or None,
                        ),
                    )
                )
        counts = Counter(r.server_request_id for r in clients)
        users = {
            r.server_request_id: r.user_id
            for r in clients
            if r.server_request_id and counts[r.server_request_id] == 1
        }
        server_events = [
            (
                r.monotonic,
                1 if r.stage == SERVER_RECEIVED else -1,
                r.request_id,
                users.get(r.request_id),
            )
            for r in servers
            if r.stage in (SERVER_RECEIVED, SERVER_COMPLETED)
        ]
        problems = self.conservation_report()
        return {
            "trusted": not problems,
            "client_records": [asdict(r) for r in clients],
            "server_records": [asdict(r) for r in servers],
            "latency_seconds": _latencies(clients),
            "clock": {
                "name": "perf_counter",
                "implementation": time.get_clock_info("perf_counter").implementation,
                "resolution_seconds": time.get_clock_info("perf_counter").resolution,
                "monotonic": time.get_clock_info("perf_counter").monotonic,
            },
            "peaks": {
                "client": _peak(client_events),
                "server": _peak(server_events),
            },
            "distinct_users_total": len({r.user_id for r in clients if r.user_id}),
            "user_count_basis": (
                "client user labels, joined to server intervals by unique response "
                "request_id; workload must establish distinct authenticated subjects"
            ),
            "server_queued": None,
            "server_executing": None,
            "stage_gap": "transport does not emit queued/executing",
            "conservation_problems": problems,
        }
