"""Observe pre-HTTP connection readiness without blocking request handlers."""

from __future__ import annotations

import threading
import time


def connection_key(reference: dict, *, reverse: bool = False) -> tuple:
    """Validate a payload-free TCP four-tuple, optionally reversing endpoints."""
    if not isinstance(reference, dict) or set(reference) != {"local", "peer"}:
        raise ValueError("invalid prepared connection")
    values = []
    for name in (("peer", "local") if reverse else ("local", "peer")):
        endpoint = reference[name]
        if (
            not isinstance(endpoint, dict)
            or set(endpoint) != {"host", "port"}
            or not isinstance(endpoint["host"], str)
            or not endpoint["host"]
            or type(endpoint["port"]) is not int
            or not 1 <= endpoint["port"] <= 65535
        ):
            raise ValueError("invalid prepared endpoint")
        values.extend((endpoint["host"], endpoint["port"]))
    return tuple(values)


class ReaderReadiness:
    """Track installed readers; only the independent control thread may wait."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._readers: dict[tuple, dict] = {}

    def clear(self) -> None:
        """Start a new drained wave, never accepting a prior wave's readers."""
        with self._condition:
            if any(row["active"] for row in self._readers.values()):
                raise RuntimeError("cannot clear active prepared readers")
            self._readers.clear()

    def installed(self, reference: dict) -> None:
        """Record immediately and return; no barrier, IPC or handler delay."""
        key = connection_key(reference)
        with self._condition:
            if key in self._readers:
                raise RuntimeError("duplicate prepared reader")
            self._readers[key] = {
                "connection_ref": reference,
                "reader_installed_ns": time.perf_counter_ns(),
                "active": True,
            }
            self._condition.notify_all()

    def closed(self, reference: dict | None) -> None:
        """Retain readiness history but refuse an already closed connection."""
        if reference is None:
            return
        with self._condition:
            row = self._readers.get(connection_key(reference))
            if row is not None:
                row["active"] = False
            self._condition.notify_all()

    def wait(self, client_references: list[dict], *, timeout: float = 3) -> dict:
        """Wait on control IPC only for exact, active reverse four-tuples."""
        if not 1 <= len(client_references) <= 64:
            raise ValueError("invalid prepared connection count")
        keys = [connection_key(row, reverse=True) for row in client_references]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate expected prepared connection")
        with self._condition:
            ready = self._condition.wait_for(
                lambda: all(key in self._readers for key in keys), timeout=timeout
            )
            if not ready:
                raise TimeoutError("prepared reader confirmation timed out")
            if set(self._readers) != set(keys) or any(
                not self._readers[key]["active"] for key in keys
            ):
                raise RuntimeError("prepared reader set is not exact and active")
            return {
                "readers": [dict(self._readers[key]) for key in keys],
                "confirmed_monotonic_ns": time.perf_counter_ns(),
            }


def verify_preparation(
    value: dict, references: list[dict], *, wave_id: str, process_id: int
) -> list[str]:
    """Validate the control acknowledgement against independently held sockets."""
    problems = []
    if (
        value.get("wave_id") != wave_id
        or type(value.get("process_id")) is not int
        or value.get("process_id") != process_id
        or type(value.get("http_started_count")) is not int
        or value.get("http_started_count") != 0
    ):
        problems.append("prepared wave/PID or pre-HTTP state mismatch")
    readers = value.get("readers")
    if not isinstance(readers, list) or len(readers) != len(references):
        return problems + ["prepared reader count mismatch"]
    try:
        expected = [connection_key(row, reverse=True) for row in references]
        actual = [connection_key(row["connection_ref"]) for row in readers]
        if len(expected) != len(set(expected)) or sorted(expected) != sorted(actual):
            problems.append("prepared reader endpoints mismatch")
        confirmed = value.get("confirmed_monotonic_ns")
        if type(confirmed) is not int or confirmed <= 0:
            problems.append("invalid prepared confirmation time")
        for reader in readers:
            stamp = reader.get("reader_installed_ns")
            if (
                reader.get("active") is not True
                or type(stamp) is not int
                or stamp <= 0
                or type(confirmed) is not int
                or stamp > confirmed
            ):
                problems.append("invalid prepared reader state/time")
    except (ValueError, KeyError, TypeError):
        problems.append("malformed prepared reader acknowledgement")
    return problems
