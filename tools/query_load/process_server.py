"""Spawned real-HTTP endpoint and independent process resource sampling.

Control traffic stays on an in-memory multiprocessing pipe. Bearer/cursor keys
are never written to evidence, process arguments, or environment variables.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from multiprocessing.connection import Connection
from collections.abc import Callable
from pathlib import Path
from typing import Any

from g15.service.http import create_query_server

from .driver import build_application
from .evidence import resource_snapshot


def _io_counters() -> dict[str, int] | None:
    """Read OS process I/O counters; they are not network byte counters."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class IOCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "read_operations",
                    "write_operations",
                    "other_operations",
                    "read_transfer_bytes",
                    "write_transfer_bytes",
                    "other_transfer_bytes",
                )
            ]

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetProcessIoCounters.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(IOCounters),
            ]
            kernel32.GetProcessIoCounters.restype = wintypes.BOOL
            counters = IOCounters()
            if kernel32.GetProcessIoCounters(
                kernel32.GetCurrentProcess(), ctypes.byref(counters)
            ):
                return {
                    name: int(getattr(counters, name)) for name, _ in counters._fields_
                }
        except (OSError, AttributeError):
            return None
    else:
        try:
            return {
                key: int(value)
                for key, value in (
                    line.split(":", 1)
                    for line in Path("/proc/self/io").read_text().splitlines()
                )
            }
        except (OSError, ValueError):
            return None
    return None


def host_snapshot() -> dict[str, Any]:
    """Report host-wide memory and working-volume facts with explicit scope."""
    result: dict[str, Any] = {
        "memory_scope": "whole host; shared with other processes",
        "physical_memory_total_bytes": None,
        "physical_memory_available_bytes": None,
        "working_volume": None,
    }
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.DWORD),
                ("memory_load_percent", wintypes.DWORD),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_pagefile", ctypes.c_ulonglong),
                ("available_pagefile", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
            kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
            value = MemoryStatus()
            value.length = ctypes.sizeof(value)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
                result["physical_memory_total_bytes"] = value.total_physical
                result["physical_memory_available_bytes"] = value.available_physical
        except (OSError, AttributeError):
            pass
    else:
        try:
            rows = {
                key: int(value.split()[0]) * 1024
                for key, value in (
                    line.split(":", 1)
                    for line in Path("/proc/meminfo").read_text().splitlines()
                )
            }
            result["physical_memory_total_bytes"] = rows.get("MemTotal")
            result["physical_memory_available_bytes"] = rows.get("MemAvailable")
        except (OSError, ValueError):
            pass
    try:
        usage = shutil.disk_usage(Path.cwd())
        result["working_volume"] = {
            "scope": "filesystem containing endpoint working directory",
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    except OSError:
        pass
    return result


def endpoint_snapshot(endpoint: str) -> dict[str, Any]:
    """Collect this endpoint process only; preserve unavailable values as null."""
    value = resource_snapshot()
    for key in (
        "endpoint_resources",
        "client_server_separable",
        "limitations",
        "scope",
    ):
        value.pop(key, None)
    observed_ns = time.perf_counter_ns()
    value.update(
        endpoint=endpoint,
        process_id=os.getpid(),
        monotonic_seconds=observed_ns / 1_000_000_000,
        monotonic_ns=observed_ns,
        clock="perf_counter",
        io_counters=_io_counters(),
        network_counters=None,
    )
    return value


class EndpointSampler:
    """Sample resources without delaying request handlers or network traffic."""

    def __init__(
        self,
        endpoint: str,
        interval_seconds: float = 0.01,
        on_sample: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._on_sample = on_sample
        self.interval_seconds = interval_seconds
        self.samples: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._host_before: dict[str, Any] | None = None

    def _capture(self) -> None:
        try:
            snapshot = endpoint_snapshot(self.endpoint)
            self.samples.append(snapshot)
            if self._on_sample is not None:
                self._on_sample(snapshot)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.errors.append(type(exc).__name__)

    def _sample(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._capture()

    def start(self) -> None:
        """Start a new load-interval sampler for the current process."""
        self._host_before = host_snapshot()
        self._capture()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        """Return raw samples, CPU deltas and explicitly sampled resource maxima."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self.errors.append("sampler_thread_not_stopped")
        self._capture()
        before = self.samples[0] if self.samples else {}
        after = self.samples[-1] if self.samples else {}
        elapsed = after.get("monotonic_seconds", 0) - before.get("monotonic_seconds", 0)
        user = after.get("cpu_user_seconds", 0) - before.get("cpu_user_seconds", 0)
        system = after.get("cpu_system_seconds", 0) - before.get(
            "cpu_system_seconds", 0
        )
        maxima = {}
        for key in ("rss_bytes", "open_fds", "open_handles"):
            observed = [
                sample[key] for sample in self.samples if sample.get(key) is not None
            ]
            maxima[key] = max(observed) if observed else None
        cpu = user + system
        first_io, last_io = before.get("io_counters"), after.get("io_counters")
        io_delta = (
            {key: last_io[key] - first_io[key] for key in first_io if key in last_io}
            if isinstance(first_io, dict) and isinstance(last_io, dict)
            else None
        )
        return {
            "host_before": self._host_before,
            "host_after": host_snapshot(),
            "process_io_delta": io_delta,
            "network_counters": None,
            "network_measurement": "per-process wire/network bytes unmeasured; no host traffic substituted",
            "endpoint": self.endpoint,
            "process_id": os.getpid(),
            "logical_cpu_count": os.cpu_count(),
            "separate_endpoint_process": True,
            "sample_interval_seconds": self.interval_seconds,
            "samples": self.samples,
            "errors": self.errors,
            "elapsed_seconds": elapsed,
            "cpu_user_delta_seconds": user,
            "cpu_system_delta_seconds": system,
            "cpu_total_delta_seconds": cpu,
            "cpu_percent_one_core": 100 * cpu / elapsed if elapsed > 0 else None,
            "cpu_percent_host_capacity": (
                100 * cpu / elapsed / os.cpu_count()
                if elapsed > 0 and os.cpu_count()
                else None
            ),
            "sampled_maxima": maxima,
            "limitations": [
                "sampled maxima are lower bounds, not exact interval high-water marks",
                "peak_rss_since_process_start_bytes covers process startup too",
                "CPU seconds use OS accounting resolution; short intervals are coarse",
                "host CPU capacity uses logical CPUs, not a host-saturation measurement",
                "client process includes orchestration and paging oracle, not network-only CPU",
                "OS process I/O may combine file/device/other operations; it is not per-process network traffic",
            ],
        }


def serve_endpoint(
    control: Connection,
    event_stream: Connection,
    runs_root: str,
    bearer_key: bytes,
    cursor_key: bytes,
    users: int,
    workers: int,
) -> None:
    """Run the unchanged HTTP/auth/SQLite path in a dedicated spawned process."""
    server = None
    sampler = None
    server_thread = None
    events: list[dict[str, object]] = []
    application_events: list[dict[str, object]] = []
    condition = threading.Condition()
    stream_lock = threading.Lock()
    stream_errors: list[str] = []

    def stream_event(kind: str, event: dict[str, object]) -> None:
        try:
            with stream_lock:
                event_stream.send({"kind": kind, "event": dict(event)})
        except (OSError, EOFError) as exc:
            stream_errors.append(type(exc).__name__)

    def application_event(event: dict[str, object]) -> None:
        stream_event("application", event)

    def transport_event(event: dict[str, object]) -> None:
        # Keep origin timestamps/pid intact. Never stamp on IPC delivery.
        with condition:
            events.append(dict(event))
            stream_event("transport", event)
            condition.notify_all()

    try:
        application, application_events = build_application(
            Path(runs_root),
            bearer_key,
            cursor_key,
            users=users,
            event_sink=application_event,
        )
        server = create_query_server(
            application, event_sink=transport_event, max_workers=workers
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        host, port = server.server_address
        control.send(
            {
                "kind": "ready",
                "base_url": f"http://{host}:{port}",
                "process_id": os.getpid(),
            }
        )
        while True:
            command = control.recv()
            if command["kind"] == "begin":
                if sampler is not None:
                    raise RuntimeError("resource interval already started")
                sampler = EndpointSampler(
                    "server", on_sample=lambda sample: stream_event("resource", sample)
                )
                sampler.start()
                control.send({"kind": "begun"})
            elif command["kind"] == "finish":
                expected = command["expected_completed"]
                with condition:
                    drained = condition.wait_for(
                        lambda: sum(
                            row.get("stage") == "response_completed" for row in events
                        )
                        >= expected,
                        timeout=10,
                    )
                    transport = list(events)
                resources = sampler.stop() if sampler is not None else None
                control.send(
                    {
                        "kind": "finished",
                        "transport_events": transport,
                        "application_events": list(application_events),
                        "resources": resources,
                        "stream_errors": list(stream_errors),
                        "drain_problems": (
                            [] if drained else ["server completion drain timed out"]
                        ),
                    }
                )
                sampler = None
            elif command["kind"] == "stop":
                break
            else:
                raise ValueError("unknown endpoint control command")
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:  # pylint: disable=broad-exception-caught
        try:
            control.send(
                {
                    "kind": "failed",
                    "error": type(exc).__name__,
                    "transport_events": list(events),
                    "application_events": list(application_events),
                    "resources": sampler.stop() if sampler is not None else None,
                }
            )
            sampler = None
        except (OSError, EOFError):
            pass
    finally:
        if sampler is not None:
            sampler.stop()
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        event_stream.close()
        control.close()
