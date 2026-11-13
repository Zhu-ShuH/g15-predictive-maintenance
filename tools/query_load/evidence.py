"""Portable, non-overwriting evidence for the D0 query harness.

Query plans below are probes of the application's actual SELECT helpers, not
plans of the independent oracle and not a trace of an entire HTTP request.
Resource values describe this shared client/server process; they cannot
establish separate endpoint resource use or a load-interval memory peak.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import platform
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from g15.query import application as query_application
from g15.storage.database import connect_readonly

_REPO_ROOT = Path(__file__).resolve().parents[2]
_QUERY_SOURCE = "src/g15/query/application.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def source_facts() -> dict[str, Any]:
    """Record source identity and safe machine facts without host paths."""
    paths: set[Path] = set()
    for directory in ("src", "tools/query_load", "tests/performance/query"):
        paths.update((_REPO_ROOT / directory).rglob("*.py"))
    paths.update((_REPO_ROOT / "tests/integration").glob("test_query_*.py"))
    for relative in (
        "tests/integration/test_query_capacity_accounting.py",
        "pyproject.toml",
        "requirements.lock.txt",
        "requirements-dev.txt",
    ):
        path = _REPO_ROOT / relative
        if path.is_file():
            paths.add(path)
    status = _git("status", "--porcelain", "--untracked-files=normal")
    return {
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": None if status is None else bool(status),
        "content_sha256": {
            path.relative_to(_REPO_ROOT).as_posix(): _sha256(path)
            for path in sorted(paths)
        },
        "machine": {
            "os": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "sqlite": sqlite3.sqlite_version,
        },
        "command_template": (
            "python -m tools.query_load.driver --serial <count> "
            "--concurrent <count> --workers <count> --output <new-directory>"
        ),
        "hash_scope": (
            "Python source in src/tools/query_load/tests/performance/query, "
            "query integration tests, build and dependency files"
        ),
    }


def resource_snapshot() -> dict[str, Any]:
    """Return cumulative CPU and point-in-time memory/handle evidence.

    Unavailable measurements remain null. Peak RSS, when available, is since
    process startup, not specifically the workload interval.
    """
    cpu = os.times()
    result: dict[str, Any] = {
        "monotonic_seconds": time.monotonic(),
        "scope": "shared_process_client_and_server",
        "client_server_separable": False,
        "cpu_user_seconds": cpu.user,
        "cpu_system_seconds": cpu.system,
        "rss_bytes": None,
        "peak_rss_since_process_start_bytes": None,
        "open_fds": None,
        "open_handles": None,
        "endpoint_resources": {"client": None, "server": None},
        "limitations": [
            "point snapshots do not establish interval resource peaks",
            "CPU seconds are cumulative; no CPU utilization or host saturation claim",
            "client and server share the process; separate endpoint use is unknown",
        ],
    }
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            handle = kernel32.GetCurrentProcess()
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(MemoryCounters),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            counters = MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                result["rss_bytes"] = counters.WorkingSetSize
                result["peak_rss_since_process_start_bytes"] = (
                    counters.PeakWorkingSetSize
                )
            kernel32.GetProcessHandleCount.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetProcessHandleCount.restype = wintypes.BOOL
            count = wintypes.DWORD()
            if kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
                result["open_handles"] = count.value
        except (OSError, AttributeError):
            result["resource_error"] = "Windows process counters unavailable"
    else:
        try:
            import resource

            maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            result["peak_rss_since_process_start_bytes"] = int(
                maximum if platform.system() == "Darwin" else maximum * 1024
            )
        except (ImportError, OSError, ValueError):
            pass
        try:
            resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
            result["rss_bytes"] = resident_pages * os.sysconf("SC_PAGE_SIZE")
            result["open_fds"] = len(list(Path("/proc/self/fd").iterdir()))
        except (OSError, ValueError, IndexError):
            pass
    return result


class _PlanConnection:
    """Execute real helper SQL while recording its exact inputs and plan."""

    def __init__(self, connection: sqlite3.Connection, run_id: str) -> None:
        self.connection = connection
        self.run_id = run_id
        self.case = ""
        self.plans: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        caller = inspect.currentframe().f_back
        try:
            source = Path(caller.f_code.co_filename).resolve()
            if source != (_REPO_ROOT / _QUERY_SOURCE).resolve():
                raise ValueError("query plan must originate in the application source")
            normalized = " ".join(sql.split())
            if not normalized.upper().startswith("SELECT "):
                raise ValueError("query plan probe accepts SELECT only")
            plan = self.connection.execute(
                "EXPLAIN QUERY PLAN " + sql, params
            ).fetchall()
            self.plans.append(
                {
                    "run_id": self.run_id,
                    "case": self.case,
                    "source_path": _QUERY_SOURCE,
                    "source_line": caller.f_lineno,
                    "source_function": caller.f_code.co_name,
                    "source_sha256": _sha256(source),
                    "sql": normalized,
                    "parameters": list(params),
                    "plan": [list(row) for row in plan],
                    "evidence_kind": "actual_application_select_helper_probe",
                }
            )
        finally:
            del caller
        return self.connection.execute(sql, params)


def query_plans(db_paths: list[Path], page_size: int = 10) -> list[dict[str, Any]]:
    """Probe first/continuation SELECT plans on committed D0 databases.

    The continuation alarm probe uses its last committed key even if D0 has
    only one alarm. It exercises the real SQL branch, not a claimed second
    alarm page. These plans omit authorization, metadata and cursor-reference
    lookup overhead and must not be represented as an entire request trace.
    """
    if not 1 <= page_size <= query_application.MAX_PAGE_SIZE:
        raise ValueError("page_size is outside the query contract")
    if (
        Path(query_application.__file__).resolve()
        != (_REPO_ROOT / _QUERY_SOURCE).resolve()
    ):
        raise ValueError("query module does not originate in this source tree")
    plans: list[dict[str, Any]] = []
    for db_path in db_paths:
        connection = connect_readonly(db_path)
        try:
            connection.execute("BEGIN")
            pairs = connection.execute(
                "SELECT DISTINCT run_id, device_id FROM feature_windows "
                "ORDER BY run_id, device_id"
            ).fetchall()
            for run_id, device_id in pairs:
                capture = _PlanConnection(connection, run_id)
                capture.case = "window_high_water"
                high_window = query_application._window_high_water(
                    capture, run_id, device_id
                )
                capture.case = "alarm_high_water"
                high_alarm = query_application._alarm_high_water(
                    capture, run_id, device_id
                )
                capture.case = "windows_first_page"
                windows = query_application._window_rows(
                    capture, run_id, device_id, page_size + 1, high_window, None
                )
                if windows:
                    capture.case = "windows_continuation_probe"
                    last_window = windows[min(page_size, len(windows)) - 1][0]
                    query_application._window_rows(
                        capture,
                        run_id,
                        device_id,
                        page_size + 1,
                        high_window,
                        last_window,
                    )
                capture.case = "alarms_first_page"
                alarms = query_application._alarm_rows(
                    capture, run_id, device_id, page_size + 1, high_alarm, None
                )
                if alarms:
                    capture.case = "alarms_continuation_branch_probe"
                    last_alarm = alarms[min(page_size, len(alarms)) - 1][:2]
                    query_application._alarm_rows(
                        capture,
                        run_id,
                        device_id,
                        page_size + 1,
                        high_alarm,
                        last_alarm,
                    )
                capture.case = "states"
                query_application._read_states(capture, run_id, device_id)
                plans.extend(capture.plans)
            connection.execute("ROLLBACK")
        finally:
            connection.close()
    return plans


def write_evidence(output: Path, report: dict[str, Any]) -> None:
    """Write report.json in a new directory, retaining success or failure.

    Existing output directories are rejected, so a later run cannot silently
    overwrite previous failure evidence. Serialization happens before directory
    creation; report.tmp remains if a subsequent write/rename fails.
    """
    payload = (
        json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    temporary = output / "report.tmp"
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(output / "report.json")
