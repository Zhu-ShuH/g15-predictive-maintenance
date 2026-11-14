"""Durable, bounded per-device plans for the public live fault injector.

The runtime owns locking and sampling; this module never reads private truth or
changes analysis results. Accepted command receipts are retained for the session.
"""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import re
import uuid

from g15.simulation.faults import FAULT_TYPES
from .schema import atomic_json, utc_now

LIMITS = {
    "fault_types": list(FAULT_TYPES),
    "max_amplitude": 10,
    "max_delay_seconds": 300,
    "max_duration_seconds": 300,
    "min_duration_points": {"spike": 1, "step": 1, "burst": 3},
    "max_history": 256,
    "max_requests": 1024,
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_BASE_FIELDS = {"session_id", "run_id", "device_id", "request_id", "action"}
_FAULT_FIELDS = {"fault_type", "amplitude", "delay_seconds", "duration_seconds"}
_TERMINAL = {"stopped", "error", "limit_reached"}


class LiveRiskError(ValueError):
    """A fixed public error without filesystem paths or exception details."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _invalid():
    return LiveRiskError(400, "invalid_live_risk_request", "Invalid live risk request")


def _validate(payload, sample_rate):
    if not isinstance(payload, dict):
        raise _invalid()
    action = payload.get("action")
    if action not in ("inject", "clear", "automatic"):
        raise _invalid()
    if set(payload) != _BASE_FIELDS | (_FAULT_FIELDS if action == "inject" else set()):
        raise _invalid()
    for key in ("session_id", "run_id", "device_id", "request_id"):
        value = payload[key]
        pattern = _REQUEST_ID if key == "request_id" else _IDENTIFIER
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise _invalid()
    if action == "inject":
        if payload["fault_type"] not in FAULT_TYPES:
            raise _invalid()
        for key, maximum, allow_zero in (
            ("amplitude", LIMITS["max_amplitude"], False),
            ("delay_seconds", LIMITS["max_delay_seconds"], True),
            ("duration_seconds", LIMITS["max_duration_seconds"], False),
        ):
            value = payload[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= maximum
                or not math.isfinite(value)
                or (not allow_zero and value == 0)
            ):
                raise _invalid()
        duration = math.ceil(payload["duration_seconds"] * sample_rate)
        if duration < LIMITS["min_duration_points"][payload["fault_type"]]:
            raise _invalid()
    return deepcopy(payload)


class LiveRiskPlans:
    """Copy-on-write plans; caller holds the runtime control/state locks."""

    def __init__(self, path: Path, session_id: str, devices, sample_rate: int):
        self.path, self.session_id, self.sample_rate = path, session_id, sample_rate
        self._data = {
            "schema_version": "1",
            "session_id": session_id,
            "version": 0,
            "devices": {
                device.run_id: {"mode": "auto", "event_id": None} for device in devices
            },
            "events": {},
            "requests": {},
        }
        atomic_json(self.path, self._data)

    def plan(self, device):
        """Return an isolated plan for the next admitted batch."""
        row = self._data["devices"][device.run_id]
        event = self._data["events"].get(row["event_id"])
        return row["mode"], deepcopy(event)

    @staticmethod
    def _event_snapshot(event, device, status, observed_at):
        value = deepcopy(event)
        end = value["end_seq_exclusive"]
        if (
            value["cancel_seq"] is None
            and device.processed_samples < end
            and status in _TERMINAL
        ):
            value.update(
                cancel_seq=device.captured_samples,
                cancelled_at_utc=observed_at,
                cancel_reason="session_stopped",
            )
        applied_end = (
            min(end, value["cancel_seq"]) if value["cancel_seq"] is not None else end
        )
        value["captured_points"] = max(
            0, min(device.captured_samples, applied_end) - value["start_seq"]
        )
        value["processed_points"] = max(
            0, min(device.processed_samples, applied_end) - value["start_seq"]
        )
        if value["cancel_seq"] is not None:
            value["status"] = "cancelled"
        elif device.processed_samples >= end:
            value["status"] = "completed"
        elif device.captured_samples >= end:
            value["status"] = "recovering"
        elif device.captured_samples > value["start_seq"]:
            value["status"] = "active"
        else:
            value["status"] = "scheduled"
        return value

    def snapshot(self, devices, status, controls_available, observed_at):
        """Project progress from actual captured and committed sample counters."""
        catalog = {device.run_id: device for device in devices}
        events = {
            key: self._event_snapshot(
                value, catalog[value["run_id"]], status, observed_at
            )
            for key, value in self._data["events"].items()
        }
        rows = []
        for device in devices:
            row = self._data["devices"][device.run_id]
            event = deepcopy(events.get(row["event_id"]))
            if event is not None:
                event.pop("run_id")
                event.pop("device_id")
            rows.append(
                {
                    "run_id": device.run_id,
                    "device_id": device.device_id,
                    "mode": row["mode"],
                    "capture_paused": device.capture_paused,
                    "event": event,
                }
            )
        return {
            "schema_version": "1",
            "session_id": self.session_id,
            "status": status,
            "version": self._data["version"],
            "controls_available": controls_available,
            "limits": deepcopy(LIMITS),
            "devices": rows,
            "history": list(reversed(list(events.values()))),
            "history_count": len(events),
        }

    def control(self, payload, devices, status, controls_available, observed_at):
        """Persist acceptance before exposing a mode, event or idempotency key."""
        body = _validate(payload, self.sample_rate)
        if body["session_id"] != self.session_id:
            raise LiveRiskError(
                409, "live_risk_session_changed", "Live session changed"
            )
        device = next(
            (
                d
                for d in devices
                if (d.run_id, d.device_id) == (body["run_id"], body["device_id"])
            ),
            None,
        )
        if device is None:
            raise LiveRiskError(
                404, "live_risk_device_not_found", "Live device not found"
            )
        previous = self._data["requests"].get(body["request_id"])
        if previous is not None:
            if previous["body"] != body:
                raise LiveRiskError(
                    409, "live_risk_request_conflict", "Request ID already used"
                )
            return {**previous["receipt"], "replayed": True}
        if not controls_available:
            raise LiveRiskError(
                409, "live_risk_not_running", "Live session is not running"
            )
        if len(self._data["requests"]) >= LIMITS["max_requests"] or (
            body["action"] == "inject"
            and len(self._data["events"]) >= LIMITS["max_history"]
        ):
            raise LiveRiskError(
                409, "live_risk_history_full", "Live risk history is full"
            )
        next_data = deepcopy(self._data)
        plan = next_data["devices"][device.run_id]
        event = next_data["events"].get(plan["event_id"])
        now = utc_now()
        if body["action"] == "inject":
            if event is not None and self._event_snapshot(
                event, device, status, observed_at
            )["status"] not in ("completed", "cancelled"):
                raise LiveRiskError(
                    409, "live_risk_busy", "Clear the current risk first"
                )
            delay_points = math.ceil(body["delay_seconds"] * self.sample_rate)
            duration_points = math.ceil(body["duration_seconds"] * self.sample_rate)
            start = device.captured_samples + delay_points
            event = {
                "event_id": "risk-" + uuid.uuid4().hex,
                "run_id": device.run_id,
                "device_id": device.device_id,
                "fault_type": body["fault_type"],
                "amplitude": body["amplitude"],
                "delay_seconds": delay_points / self.sample_rate,
                "duration_seconds": duration_points / self.sample_rate,
                "start_seq": start,
                "end_seq_exclusive": start + duration_points,
                "duration_points": duration_points,
                "created_at_utc": now,
                "cancel_seq": None,
                "cancelled_at_utc": None,
                "cancel_reason": None,
            }
            next_data["events"][event["event_id"]] = event
            plan.update(mode="normal", event_id=event["event_id"])
        else:
            if (
                event is not None
                and event["cancel_seq"] is None
                and device.captured_samples < event["end_seq_exclusive"]
            ):
                event.update(
                    cancel_seq=device.captured_samples,
                    cancelled_at_utc=now,
                    cancel_reason=(
                        "cleared" if body["action"] == "clear" else "automatic"
                    ),
                )
            plan["mode"] = "auto" if body["action"] == "automatic" else "normal"
        receipt = {
            "request_id": body["request_id"],
            "action": body["action"],
            "event_id": plan["event_id"],
            "replayed": False,
        }
        next_data["version"] += 1
        next_data["requests"][body["request_id"]] = {
            "body": body,
            "receipt": receipt,
            "accepted_at_utc": now,
            "captured_seq": device.captured_samples,
        }
        try:
            atomic_json(self.path, next_data)
        except OSError as exc:
            raise LiveRiskError(
                503, "live_risk_persistence_failed", "Risk plan could not be saved"
            ) from exc
        self._data = next_data
        return deepcopy(receipt)
