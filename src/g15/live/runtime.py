"""Wall-clock simulated capture with durable queues and one analysis writer.

This module does not read private truth and never publishes a complete Manifest.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import traceback
import uuid

import numpy as np

from g15.analysis.features import extract_features
from g15.analysis.health import load_health_rules, score_health
from g15.analysis.alarms import AlarmState, evaluate_alarm
from g15.simulation.engine import simulate_chunk
from g15.storage.database import connect, initialize_database, transaction
from g15.storage.repository import (
    ConsumptionRepository,
    encode_features_json,
    encode_state_json,
)
from .schema import SCHEMA_VERSION, SCENARIO_ID, atomic_json, atomic_replace, utc_now


@dataclass(frozen=True)
class LiveConfig:
    """Finite public capture limits; nominal sampling geometry is explicit."""

    devices: int = 10
    sample_rate_hz: int = 256
    window_points: int = 256
    interval_seconds: float = 1.0
    queue_capacity: int = 20
    max_duration_seconds: float = 3600.0
    max_disk_bytes: int = 536870912
    seed: int = 160
    cycle_windows: int = 12
    backpressure_timeout_seconds: float = 2.0

    def __post_init__(self):
        for key in (
            "devices",
            "sample_rate_hz",
            "window_points",
            "queue_capacity",
            "max_disk_bytes",
            "seed",
            "cycle_windows",
        ):
            if type(getattr(self, key)) is not int:
                raise ValueError("live integer configuration required")
        if (
            not 1 <= self.devices <= 10
            or not 1 <= self.sample_rate_hz <= 4096
            or not 2 <= self.window_points <= 4096
        ):
            raise ValueError("live device/sample/window limit exceeded")
        if (
            not 1 <= self.queue_capacity <= 200
            or not 4 <= self.cycle_windows <= 3600
            or self.seed < 0
        ):
            raise ValueError("invalid live queue/profile configuration")
        for key in (
            "interval_seconds",
            "max_duration_seconds",
            "backpressure_timeout_seconds",
        ):
            value = getattr(self, key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("finite positive live duration required")
        if (
            not 0.02 <= self.interval_seconds <= 5
            or self.max_duration_seconds > 86400
            or self.backpressure_timeout_seconds > 30
        ):
            raise ValueError("live duration limit exceeded")
        count = self.sample_rate_hz * self.interval_seconds
        if (
            abs(count - round(count)) > 1e-8
            or not 1 <= round(count) // self.window_points <= 16
            or round(count) % self.window_points
        ):
            raise ValueError(
                "each interval must contain one to sixteen complete windows"
            )
        if not 4 * 1024**2 <= self.max_disk_bytes <= 4 * 1024**3:
            raise ValueError("live disk budget must be 4 MiB through 4 GiB")

    @property
    def batch_points(self) -> int:
        """Number of fresh absolute-sequence samples produced each tick."""
        return round(self.sample_rate_hz * self.interval_seconds)


@dataclass
class _Device:
    run_id: str
    device_id: str
    captured_samples: int = 0
    processed_samples: int = 0
    windows_written: int = 0
    alarms_written: int = 0
    queue_depth: int = 0
    last_capture_at_utc: str | None = None
    last_commit_at_utc: str | None = None
    error: str | None = None
    latency_count: int = 0
    latency_sum: float = 0.0
    latency_max: float = 0.0


@dataclass(frozen=True)
class _Task:
    index: int
    start_seq: int
    count: int
    path: Path
    sha256: str
    captured_monotonic_ns: int
    captured_at_utc: str
    generation_seconds: float


class _WriterLock:
    def __init__(self, path):
        self.path, self.handle = path, None

    def acquire(self):
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # pylint: disable=import-error

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise RuntimeError("another live writer owns this output root") from None

    def release(self):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # pylint: disable=import-error

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _resource_sample():
    value = {
        "observed_at_utc": utc_now(),
        "monotonic_ns": time.perf_counter_ns(),
        "process_id": os.getpid(),
        "cpu_user_seconds": os.times().user,
        "cpu_system_seconds": os.times().system,
        "rss_bytes": None,
        "open_fds_or_handles": None,
    }
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Memory(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ]

        kernel, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL(
            "psapi", use_last_error=True
        )
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        handle = kernel.GetCurrentProcess()
        memory = Memory()
        memory.cb = ctypes.sizeof(memory)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Memory),
            wintypes.DWORD,
        ]
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
            value["rss_bytes"] = int(memory.WorkingSetSize)
        count = wintypes.DWORD()
        kernel.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        if kernel.GetProcessHandleCount(handle, ctypes.byref(count)):
            value["open_fds_or_handles"] = count.value
    elif Path("/proc/self/statm").exists():
        value["rss_bytes"] = int(
            Path("/proc/self/statm").read_text(encoding="ascii").split()[1]
        ) * os.sysconf("SC_PAGE_SIZE")
        value["open_fds_or_handles"] = len(list(Path("/proc/self/fd").iterdir()))
    return value


class LiveRuntime:
    """A new durable live simulation session; existing sessions are never reset."""

    def __init__(
        self,
        output_root: Path | str,
        *,
        config: LiveConfig | None = None,
        health_rules_path: Path | str,
    ):
        self.config = config or LiveConfig()
        self.rules = load_health_rules(health_rules_path)
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.session_id = (
            "live-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid.uuid4().hex[:10]
        )
        self.session_root = self.output_root / self.session_id
        self.session_root.mkdir()
        self.runs_root = self.session_root / "runs"
        self.runs_root.mkdir()
        self.catalog_path = self.session_root / "catalog.json"
        self._lock = threading.RLock()
        self._writer_lock = _WriterLock(self.output_root / ".live-writer.lock")
        self._queue = queue.Queue(maxsize=self.config.queue_capacity)
        self._stop = threading.Event()
        self._producer_done = threading.Event()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._writer_failed = threading.Event()
        self._threads = []
        self._started = False
        self._state = "starting"
        self._error = None
        self._limit_reason = None
        self._started_at = utc_now()
        self._updated_at = self._started_at
        self._origin = None
        self._raw_bytes = 0
        self._backpressure_events = 0
        self._late_cycles = 0
        self._latencies = deque(maxlen=2048)
        self._resource_rows = deque(maxlen=120)
        self._resource_max = {}
        self._devices = [
            _Device(f"{self.session_id}-d{index+1:02d}", f"LIVE-{index+1:03d}")
            for index in range(self.config.devices)
        ]
        for device in self._devices:
            directory = self.runs_root / device.run_id
            for name in ("raw", "artifacts", "reports"):
                (directory / name).mkdir(parents=True)
        atomic_json(self.catalog_path, self.catalog())
        atomic_json(
            self.session_root / "capture-config.json",
            {
                "schema_version": "1",
                "source_kind": "wall_clock_simulator",
                "config": asdict(self.config),
                "profiles": {
                    "normal": {"vibration_baseline": 0.5, "noise_std": 0.05},
                    "degradation": "rising baseline and noise during public cycle",
                    "abnormal": {"vibration_baseline": 4.5, "noise_std": 1.2},
                    "recovery": "normal baseline/noise after abnormal phase",
                },
                "health_rules": asdict(self.rules),
            },
        )
        self._persist_all()

    def catalog(self) -> list[dict]:
        """Return only public current resource metadata, without paths/keys."""
        with self._lock:
            return [
                {
                    "run_id": d.run_id,
                    "device_id": d.device_id,
                    "scenario": "live",
                    "total_points": d.captured_samples,
                }
                for d in self._devices
            ]

    def _descriptor(self, device):
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "run_id": device.run_id,
            "device_id": device.device_id,
            "scenario_id": SCENARIO_ID,
            "source_kind": "wall_clock_simulator",
            "sample_rate_hz": self.config.sample_rate_hz,
            "window_points": self.config.window_points,
            "interval_seconds": self.config.interval_seconds,
            "started_at_utc": self._started_at,
            "updated_at_utc": self._updated_at,
            "status": self._state,
            "captured_samples": device.captured_samples,
            "processed_samples": device.processed_samples,
            "windows_written": device.windows_written,
            "alarms_written": device.alarms_written,
            "scoring_version": self.rules.scoring_version,
            "rule_id": self.rules.rule_id,
            "error": device.error or self._error,
        }

    def _persist_all(self):
        with self._lock:
            self._updated_at = utc_now()
            for device in self._devices:
                atomic_json(
                    self.runs_root / device.run_id / "live-session.json",
                    self._descriptor(device),
                )
            atomic_json(
                self.session_root / "session-status.json", self.status_snapshot()
            )

    def status_snapshot(self) -> dict:
        """Read a coherent status; counters represent actual capture and commits."""
        with self._lock:
            rows = []
            for d in self._devices:
                row = {
                    key: getattr(d, key)
                    for key in (
                        "run_id",
                        "device_id",
                        "captured_samples",
                        "processed_samples",
                        "windows_written",
                        "alarms_written",
                        "queue_depth",
                        "last_capture_at_utc",
                        "last_commit_at_utc",
                        "error",
                    )
                }
                row["pending_samples"] = d.captured_samples - d.processed_samples
                row["latency"] = {
                    "count": d.latency_count,
                    "mean_seconds": (
                        d.latency_sum / d.latency_count if d.latency_count else None
                    ),
                    "max_seconds": d.latency_max if d.latency_count else None,
                }
                rows.append(row)
            values = sorted(self._latencies)
            return {
                "schema_version": "1",
                "session_id": self.session_id,
                "status": self._state,
                "started_at_utc": self._started_at,
                "updated_at_utc": self._updated_at,
                "sample_rate_hz": self.config.sample_rate_hz,
                "window_points": self.config.window_points,
                "interval_seconds": self.config.interval_seconds,
                "source_kind": "wall_clock_simulator",
                "devices": rows,
                "error": self._error,
                "limit_reason": self._limit_reason,
                "backpressure_events": self._backpressure_events,
                "late_cycles": self._late_cycles,
                "latency": {
                    "basis": (
                        "actual sample generation start to successful SQLite commit "
                        "return; not hardware sampling latency"
                    ),
                    "recent_count": len(values),
                    "recent_p95_seconds": (
                        values[math.ceil(0.95 * len(values)) - 1] if values else None
                    ),
                },
                "resources": {
                    "scope": (
                        "whole current process, including any colocated HTTP "
                        "console/query"
                    ),
                    "latest": self._resource_rows[-1] if self._resource_rows else None,
                    "observed_max": dict(self._resource_max),
                },
            }

    def start(self):
        """Start once; database initialization remains inside the sole writer."""
        if self._started or self._done.is_set():
            raise RuntimeError("a live session cannot be restarted or reset")
        self._writer_lock.acquire()
        self._started = True
        worker = threading.Thread(
            target=self._analyse, name="g15-live-analysis", daemon=True
        )
        self._threads.append(worker)
        worker.start()
        if not self._ready.wait(15) or self._writer_failed.is_set():
            self._stop.set()
            self._producer_done.set()
            raise RuntimeError("live initialization failed")
        with self._lock:
            self._origin = time.monotonic()
            self._state = "running"
        producer = threading.Thread(
            target=self._capture, name="g15-live-capture", daemon=True
        )
        monitor = threading.Thread(
            target=self._monitor, name="g15-live-monitor", daemon=True
        )
        self._threads += [producer, monitor]
        producer.start()
        monitor.start()
        return self

    def wait(self, timeout=None) -> bool:
        """Wait until final reports, writer closure and lock release complete."""
        return self._done.wait(timeout)

    def stop(self, timeout=10) -> dict:
        """Stop new capture, drain durable accepted work, and wait up to timeout."""
        if not self._started:
            with self._lock:
                self._state = "stopped"
            self._producer_done.set()
            self._done.set()
            self._persist_all()
            return self.status_snapshot()
        with self._lock:
            if self._state in ("starting", "running"):
                self._state = "stopping"
        self._stop.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
        if not self._done.is_set():
            with self._lock:
                self._state = "stopping"
                self._error = "stop_timeout"
            self._persist_all()
        return self.status_snapshot()

    def _fail(self, code, exc=None, index=None):
        if exc is not None:
            self._record_error(code, exc)
        with self._lock:
            self._error = code
            self._state = "error"
            if index is not None:
                self._devices[index].error = code
        self._stop.set()

    def _record_error(self, code, exc):
        # Private evidence is never served; do not serialize exception messages.
        record = {
            "observed_at_utc": utc_now(),
            "code": code,
            "exception_type": type(exc).__name__,
            "frames": [
                {
                    "file": Path(frame.filename).name,
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in traceback.extract_tb(exc.__traceback__)
            ],
        }
        try:
            with self._lock:
                with (self.session_root / "runtime-errors.ndjson").open(
                    "a", encoding="utf-8"
                ) as stream:
                    stream.write(json.dumps(record, sort_keys=True) + "\n")
                    stream.flush()
        except OSError:
            pass  # State still reports the failure when the evidence disk fails.

    def _disk_usage_bytes(self):
        total = self._raw_bytes
        for file in self.session_root.iterdir():
            if file.is_file():
                total += file.stat().st_size
        for d in self._devices:
            directory = self.runs_root / d.run_id
            for file in [
                directory / "live-session.json",
                *list((directory / "artifacts").glob("*")),
                *list((directory / "reports").glob("*")),
            ]:
                if file.is_file():
                    total += file.stat().st_size
        return total

    def _capture(self):
        deadline = self._origin + self.config.interval_seconds
        try:
            while not self._stop.is_set():
                until_limit = self._origin + self.config.max_duration_seconds
                if self._stop.wait(
                    max(0, min(deadline, until_limit) - time.monotonic())
                ):
                    break
                if time.monotonic() >= until_limit:
                    self._limit_reason = "duration_limit"
                    break
                reserve = (self._queue.qsize() + 1) * (
                    1024**2 + self.config.batch_points * 40
                )
                if (
                    self._disk_usage_bytes() + reserve >= self.config.max_disk_bytes
                    or shutil.disk_usage(self.session_root).free
                    < reserve + 16 * 1024**2
                ):
                    self._limit_reason = "disk_limit"
                    break
                for index in range(self.config.devices):
                    if self._stop.is_set():
                        break
                    if time.monotonic() >= until_limit:
                        self._limit_reason = "duration_limit"
                        self._stop.set()
                        break
                    if self._disk_usage_bytes() + reserve >= self.config.max_disk_bytes:
                        self._limit_reason = "disk_limit"
                        self._stop.set()
                        break
                    task = self._capture_one(index)
                    waiting = time.monotonic()
                    recorded = False
                    while not self._writer_failed.is_set():
                        try:
                            with self._lock:
                                self._queue.put_nowait(task)
                                self._devices[index].queue_depth += 1
                            break
                        except queue.Full:
                            if not recorded:
                                self._backpressure_events += 1
                                recorded = True
                            if (
                                time.monotonic() - waiting
                                > self.config.backpressure_timeout_seconds
                            ):
                                self._fail("backpressure_timeout", index=index)
                            time.sleep(0.01)
                    else:
                        self._fail("analysis_unavailable", index=index)
                        break
                deadline += self.config.interval_seconds
                if deadline < time.monotonic():
                    self._late_cycles += 1
                    deadline = time.monotonic() + self.config.interval_seconds
        except Exception as exc:
            self._fail("capture_error", exc)
        finally:
            self._producer_done.set()
            self._stop.set()

    def _capture_one(self, index):
        device = self._devices[index]
        with self._lock:
            start = device.captured_samples
        began = time.perf_counter_ns()
        captured_at = utc_now()
        arrays = {
            key: [] for key in ("seq", "timestamp_ns", "vibration", "temperature")
        }
        for offset in range(0, self.config.batch_points, self.config.window_points):
            position = start + offset
            phase = (
                ((position // self.config.window_points) + index)
                % self.config.cycle_windows
                / self.config.cycle_windows
            )
            if phase < 0.25 or phase >= 0.75:
                baseline, noise = 0.5, 0.05
            elif phase < 0.5:
                baseline, noise = 0.5 + (phase - 0.25) * 4, 0.05 + (phase - 0.25) * 0.4
            else:
                baseline, noise = 4.5, 1.2
            params = {
                "scenario": {
                    "seed": self.config.seed + index,
                    "sample_rate_hz": self.config.sample_rate_hz,
                    "start_time_utc": self._started_at,
                    "total_points": max(
                        1,
                        math.ceil(
                            self.config.max_duration_seconds
                            * self.config.sample_rate_hz
                        )
                        + self.config.batch_points,
                    ),
                    "device_id": device.device_id,
                },
                "signals": {
                    "vibration": {
                        "baseline": baseline,
                        "noise_std": noise,
                        "degradation_rate_per_sample": 0.0,
                        "unit": "g",
                    },
                    "temperature": {
                        "baseline": 25.0,
                        "noise_std": 0.1,
                        "degradation_rate_per_sample": 0.0,
                        "unit": "degC",
                    },
                },
            }
            chunk = simulate_chunk(params, position, self.config.window_points)
            for key in arrays:
                arrays[key].append(getattr(chunk, key))
        path = (
            self.runs_root
            / device.run_id
            / "raw"
            / f"part-{start//self.config.batch_points:08d}.npz"
        )
        temporary = path.with_suffix(".pending-" + uuid.uuid4().hex)
        with temporary.open("xb") as stream:
            np.savez_compressed(
                stream, **{key: np.concatenate(value) for key, value in arrays.items()}
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self._lock:
            self._raw_bytes += path.stat().st_size
            device.captured_samples += self.config.batch_points
            device.last_capture_at_utc = captured_at
            self._persist_all()
        return _Task(
            index,
            start,
            self.config.batch_points,
            path,
            digest,
            began,
            captured_at,
            (time.perf_counter_ns() - began) / 1e9,
        )

    def _analyse(self):
        connections = {}
        repositories = {}
        states = {}
        try:
            for index, device in enumerate(self._devices):
                conn = connect(
                    self.runs_root / device.run_id / "artifacts" / "results.sqlite"
                )
                initialize_database(conn)
                connections[index] = conn
                repository = ConsumptionRepository(conn)
                repository.ensure_run(
                    device.run_id,
                    SCENARIO_ID,
                    SCHEMA_VERSION,
                    self.rules.scoring_version,
                    "streaming",
                    self._started_at,
                )
                repositories[index] = repository
                states[index] = AlarmState()
                self._write_report(index, conn)
            self._ready.set()
            while True:
                try:
                    task = self._queue.get(timeout=0.05)
                except queue.Empty:
                    if self._producer_done.is_set():
                        break
                    continue
                with self._lock:
                    self._devices[task.index].queue_depth -= 1
                try:
                    states[task.index] = self._process_task(
                        task, repositories[task.index], states[task.index]
                    )
                    self._write_report(task.index, connections[task.index])
                finally:
                    self._queue.task_done()
        except Exception as exc:
            self._writer_failed.set()
            self._fail("analysis_error", exc)
        finally:
            self._ready.set()
            self._stop.set()
            # Producer may still be persisting one capture; wait before final counts.
            while self._started and not self._producer_done.wait(0.01):
                if not any(t.name == "g15-live-capture" for t in self._threads):
                    self._producer_done.set()
            with self._lock:
                self._state = (
                    "error"
                    if self._error
                    else "limit_reached" if self._limit_reason else "stopped"
                )
            try:
                for index, conn in connections.items():
                    self._write_report(index, conn)
                self._persist_all()
            except Exception as exc:
                self._fail("finalize_error", exc)
            finally:
                for conn in connections.values():
                    conn.close()
                self._writer_lock.release()
                self._done.set()

    def _process_task(self, task, repository, state):
        if hashlib.sha256(task.path.read_bytes()).hexdigest() != task.sha256:
            raise ValueError("captured task digest changed")
        analysis_started = time.perf_counter_ns()
        with np.load(task.path, allow_pickle=False) as raw:
            if set(raw.files) != {"seq", "timestamp_ns", "vibration", "temperature"}:
                raise ValueError("invalid captured keys")
            # NumPy NPZ stubs are incorrectly inferred as numpy.array functions.
            # pylint: disable=no-member,unsubscriptable-object
            seq, vibration = raw["seq"], raw["vibration"]
            timestamps, temperature = raw["timestamp_ns"], raw["temperature"]
            if (
                seq.dtype != np.dtype("int64")
                or not np.array_equal(
                    seq,
                    np.arange(
                        task.start_seq, task.start_seq + task.count, dtype=np.int64
                    ),
                )
                or vibration.dtype != np.dtype("float64")
                or vibration.shape != (task.count,)
                or not np.isfinite(vibration).all()
                or timestamps.dtype != np.dtype("int64")
                or timestamps.shape != (task.count,)
                or not np.all(np.diff(timestamps) > 0)
                or temperature.dtype != np.dtype("float64")
                or temperature.shape != (task.count,)
                or not np.isfinite(temperature).all()
            ):
                raise ValueError("captured geometry changed")
            analysed = []
            for offset in range(0, task.count, self.config.window_points):
                start = task.start_seq + offset
                features = extract_features(
                    vibration[offset : offset + self.config.window_points],
                    self.config.sample_rate_hz,
                    window_start_seq=start,
                )
                health = score_health(features, self.rules)
                alarms, state = evaluate_alarm(health, state, self.rules)
                analysed.append((start, features, health, alarms, state))
        # pylint: enable=no-member,unsubscriptable-object
        device = self._devices[task.index]
        if device.processed_samples != task.start_seq:
            raise ValueError("captured task gap or replay")
        with transaction(repository.get_connection()):
            repository.mark_chunk_processed(
                device.run_id,
                task.start_seq // self.config.batch_points,
                task.sha256,
                utc_now(),
            )
            for start, features, health, alarms, next_state in analysed:
                repository.insert_feature_window(
                    device.run_id,
                    device.device_id,
                    start,
                    start + self.config.window_points,
                    encode_features_json(features, health, window_start_seq=start),
                    health.health_score,
                )
                for alarm in alarms:
                    repository.insert_alarm(
                        device.run_id,
                        device.device_id,
                        alarm.rule_id,
                        start,
                        alarm.level,
                        alarm.reason,
                        utc_now(),
                    )
                repository.upsert_alarm_state(
                    device.run_id,
                    device.device_id,
                    self.rules.rule_id,
                    start + self.config.window_points,
                    encode_state_json(next_state),
                )
        committed = time.perf_counter_ns()
        latency = (committed - task.captured_monotonic_ns) / 1e9
        record = {
            "run_id": device.run_id,
            "device_id": device.device_id,
            "start_seq": task.start_seq,
            "samples": task.count,
            "capture_started_monotonic_ns": task.captured_monotonic_ns,
            "analysis_started_monotonic_ns": analysis_started,
            "committed_monotonic_ns": committed,
            "capture_to_commit_seconds": latency,
            "generation_seconds": task.generation_seconds,
            "committed_at_utc": utc_now(),
        }
        with self._lock:
            device.processed_samples += task.count
            device.windows_written += len(analysed)
            device.alarms_written += sum(len(row[3]) for row in analysed)
            device.last_commit_at_utc = record["committed_at_utc"]
            device.latency_count += 1
            device.latency_sum += latency
            device.latency_max = max(device.latency_max, latency)
            self._latencies.append(latency)
            with (self.session_root / "latency.ndjson").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()
            self._persist_all()
        return state

    def _write_report(self, index, connection):
        device = self._devices[index]
        connection.execute("BEGIN")
        try:
            count, minimum, maximum, average = connection.execute(
                "SELECT COUNT(*),MIN(health_score),MAX(health_score),AVG(health_score) "
                "FROM feature_windows WHERE run_id=?",
                (device.run_id,),
            ).fetchone()
            alarms = connection.execute(
                "SELECT COUNT(*) FROM alarms WHERE run_id=?", (device.run_id,)
            ).fetchone()[0]
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        payload = {
            "report_schema_version": "live-1",
            "run_id": device.run_id,
            "device_id": device.device_id,
            "scenario_id": SCENARIO_ID,
            "scenario_kind": None,
            "source_kind": "wall_clock_simulator",
            "generated_at_utc": utc_now(),
            "verifiable": False,
            "passed": None,
            "detection_rate": None,
            "false_alarm_count": None,
            "fault_events": None,
            "detected_events": None,
            "missed_events": None,
            "redundant_alarm_count": None,
            "unmatched_alarm_count": None,
            "feature_windows": count,
            "alarm_count": alarms,
            "health_score_min": minimum,
            "health_score_max": maximum,
            "health_score_mean": average,
            "dropped_points": 0,
            "verdict_reasons": [
                "Live simulation has no independent truth/held-out acceptance evidence."
            ],
            "captured_samples": device.captured_samples,
            "processed_samples": device.processed_samples,
            "windows_written": count,
            "alarms_written": alarms,
            "health_summary": {
                "count": count,
                "minimum": minimum,
                "maximum": maximum,
                "mean": average,
            },
            "live_status": self._state,
        }
        reports = self.runs_root / device.run_id / "reports"
        atomic_json(reports / "summary.json", payload)
        text = (
            f"# Live session summary\n\n| run_id | `{device.run_id}` |\n"
            f"| device_id | `{device.device_id}` |\n\n"
            "verifiable: false; passed: null; detection_rate: null.\n\n"
            f"Captured samples: {device.captured_samples}; committed samples: "
            f"{device.processed_samples}; windows: {count}; alarms: {alarms}.\n\n"
            "No independent truth or formal detection claim.\n"
        )
        target = reports / "summary.md"
        temporary = target.with_name("summary.md.tmp-" + uuid.uuid4().hex)
        temporary.write_text(text, encoding="utf-8")
        atomic_replace(temporary, target)

    def _monitor(self):
        while not self._done.is_set():
            try:
                row = _resource_sample()
                with self._lock:
                    if self._resource_rows:
                        previous = self._resource_rows[-1]
                        elapsed = (row["monotonic_ns"] - previous["monotonic_ns"]) / 1e9
                        cpu = (
                            row["cpu_user_seconds"]
                            + row["cpu_system_seconds"]
                            - previous["cpu_user_seconds"]
                            - previous["cpu_system_seconds"]
                        )
                        row["cpu_percent_host_capacity"] = (
                            100 * cpu / elapsed / (os.cpu_count() or 1)
                            if elapsed > 0
                            else None
                        )
                    self._resource_rows.append(row)
                    for key in (
                        "rss_bytes",
                        "open_fds_or_handles",
                        "cpu_percent_host_capacity",
                    ):
                        if isinstance(row.get(key), (int, float)):
                            self._resource_max[key] = max(
                                self._resource_max.get(key, 0), row[key]
                            )
                    with (self.session_root / "resources.ndjson").open(
                        "a", encoding="utf-8"
                    ) as stream:
                        stream.write(json.dumps(row, sort_keys=True) + "\n")
                        stream.flush()
                    self._persist_all()
            except Exception as exc:
                self._fail("monitor_error", exc)
            if self._done.wait(0.25):
                break
