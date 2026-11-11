"""Trusted client and server in-flight accounting for Gate 0 (Issue 136).

The transport (g15.service.http) emits only three stages per request:
server_received, rejected and response_completed.  It does NOT emit
server_queued or server_executing.  server_in_flight is therefore approximated
as (server_received - response_completed); the queued/executing split is a
documented gap, never a fabricated number.  The client side records
client_started / client_waiting / client_completed around the real HTTP call,
and client_in_flight is (client_started - client_completed) counted live, which
is the only honest measure of "simultaneously in flight" at Gate 0.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
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


@dataclass
class ServerRecord:
    request_id: str
    stage: str
    result: str
    timestamp_utc: str
    monotonic: float


@dataclass
class RunAccounting:
    """Live in-flight counters plus per-request records for one wave."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    client_records: list[ClientRecord] = field(default_factory=list)
    server_records: list[ServerRecord] = field(default_factory=list)
    _client_in_flight: int = 0
    _server_in_flight: int = 0
    peak_client_in_flight: int = 0
    peak_server_in_flight: int = 0

    def client_start(self) -> None:
        with self.lock:
            self._client_in_flight += 1
            self.peak_client_in_flight = max(
                self.peak_client_in_flight, self._client_in_flight
            )

    def client_complete(self, record: ClientRecord) -> None:
        with self.lock:
            self.client_records.append(record)
            self._client_in_flight -= 1

    def server_event(self, event: Mapping[str, object]) -> None:
        request_id = str(event["request_id"])
        stage = str(event["stage"])
        result = str(event.get("result", ""))
        with self.lock:
            if stage == SERVER_RECEIVED:
                self._server_in_flight += 1
                self.peak_server_in_flight = max(
                    self.peak_server_in_flight, self._server_in_flight
                )
            elif stage == SERVER_COMPLETED:
                self._server_in_flight -= 1
            self.server_records.append(
                ServerRecord(
                    request_id=request_id,
                    stage=stage,
                    result=result,
                    timestamp_utc=str(event.get("timestamp_utc", "")),
                    monotonic=time.monotonic(),
                )
            )

    def conservation_report(self) -> list[str]:
        """Return a list of accounting-conservation violations; empty is good."""
        problems: list[str] = []
        client_total = len(self.client_records)
        received = sum(1 for r in self.server_records if r.stage == SERVER_RECEIVED)
        completed = sum(1 for r in self.server_records if r.stage == SERVER_COMPLETED)
        if self._client_in_flight != 0:
            problems.append(
                f"client_in_flight nonzero at end: {self._client_in_flight}"
            )
        if self._server_in_flight != 0:
            problems.append(
                f"server_in_flight nonzero at end: {self._server_in_flight}"
            )
        if received != client_total:
            problems.append(
                f"server_received {received} != client completed {client_total}"
            )
        if completed != received:
            problems.append(
                f"response_completed {completed} != server_received {received}"
            )
        # Every client record must map to exactly one server request_id, and
        # that request_id must equal the trace_id the server echoed.
        client_ids = sorted(r.server_request_id for r in self.client_records)
        server_ids = sorted(
            r.request_id for r in self.server_records if r.stage == SERVER_RECEIVED
        )
        if client_ids != server_ids:
            problems.append("client/server request_id sets do not match")
        if any(r.server_request_id == "" for r in self.client_records):
            problems.append("a client record has no server request_id")
        return problems
