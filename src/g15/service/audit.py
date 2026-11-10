"""Request tracing for the query service.

Implements ADR-0007 section 5.1.7: every request gets a request_id (equal to
the trace_id in the first version) and records a fixed sequence of stage
events. The event list is deliberately redaction-safe: no stage carries the
token, the Authorization header, the signature, the raw subject, any truth
value, run/device identity, cursor payload, reset audit or alarm reason, so
the trace can be logged and shipped without leaking anything sensitive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid

STAGE_SERVER_RECEIVED = "server_received"
STAGE_AUTHN_CHECKED = "authn_checked"
STAGE_AUTHZ_CHECKED = "authz_checked"
STAGE_DB_OPENED = "db_opened"
STAGE_QUERY_STARTED = "query_started"
STAGE_QUERY_COMPLETED = "query_completed"
STAGE_RESPONSE_COMPLETED = "response_completed"
STAGE_REJECTED = "rejected"


@dataclass(frozen=True)
class TraceEvent:
    stage: str
    at_epoch_ms: int


@dataclass
class Trace:
    request_id: str
    query_shape: str
    events: list[TraceEvent] = field(default_factory=list)

    def record(self, stage: str) -> None:
        self.events.append(TraceEvent(stage=stage, at_epoch_ms=_now_ms()))

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "query_shape": self.query_shape,
            "events": [
                {"stage": e.stage, "at_epoch_ms": e.at_epoch_ms} for e in self.events
            ],
        }


def new_trace(query_shape: str = "single_run_single_device_health_v1") -> Trace:
    """Create a trace for one request, stamped server_received."""
    trace = Trace(request_id=str(uuid.uuid4()), query_shape=query_shape)
    trace.record(STAGE_SERVER_RECEIVED)
    return trace


def _now_ms() -> int:
    return int(time.time() * 1000)
