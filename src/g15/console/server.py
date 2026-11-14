"""Read-only loopback console: catalog, authenticated HTTP proxy and reports."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import http.client
import io
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
from importlib import resources
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

_LOGGER = logging.getLogger(__name__)
from .notifications import NotificationRequestError
from .laboratory import LaboratoryController, LaboratoryRequestError
from .waveforms import WaveformRequestError, read_waveform
from .operations import OperationsController, OperationsRequestError

MAX_HEAD_BYTES = 32_768
MAX_TARGET_BYTES = 8_192
MAX_CATALOG_BYTES = 131_072
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOKEN_BYTES = 8_192
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
ASSETS = {
    "/assets/fonts/g15-display.woff2": ("fonts/g15-display.woff2", "font/woff2"),
    "/assets/fonts/g15-instrument.woff2": ("fonts/g15-instrument.woff2", "font/woff2"),
    "/assets/fonts/g15-ui.woff2": ("fonts/g15-ui.woff2", "font/woff2"),
    "/": ("index.html", "text/html; charset=utf-8"),
    "/screen": ("screen.html", "text/html; charset=utf-8"),
    "/operations": ("operations.html", "text/html; charset=utf-8"),
    "/diagnosis": ("diagnosis.html", "text/html; charset=utf-8"),
    "/simulation": ("simulation.html", "text/html; charset=utf-8"),
    "/laboratory": ("laboratory.html", "text/html; charset=utf-8"),
    "/workbench": ("workbench.html", "text/html; charset=utf-8"),
    "/assets/workbench.js": ("workbench.js", "text/javascript; charset=utf-8"),
    "/assets/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
    "/assets/laboratory.js": ("laboratory.js", "text/javascript; charset=utf-8"),
    "/assets/laboratory.css": ("laboratory.css", "text/css; charset=utf-8"),
    "/assets/simulation.js": ("simulation.js", "text/javascript; charset=utf-8"),
    "/assets/simulation.css": ("simulation.css", "text/css; charset=utf-8"),
    "/assets/industrial.css": ("industrial.css", "text/css; charset=utf-8"),
    "/assets/shell.js": ("shell.js", "text/javascript; charset=utf-8"),
    "/assets/g15-logo.png": ("g15-logo.png", "image/png"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/notifications.js": ("notifications.js", "text/javascript; charset=utf-8"),
    "/assets/notifications.css": ("notifications.css", "text/css; charset=utf-8"),
    "/assets/state.js": ("state.js", "text/javascript; charset=utf-8"),
    "/assets/fleet.css": ("fleet.css", "text/css; charset=utf-8"),
    "/assets/fleet.js": ("fleet.js", "text/javascript; charset=utf-8"),
    "/assets/diagnosis.css": ("diagnosis.css", "text/css; charset=utf-8"),
    "/assets/diagnosis.js": ("diagnosis.js", "text/javascript; charset=utf-8"),
    "/assets/cyber-wafuu.css": ("cyber-wafuu.css", "text/css; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
}


class ConsoleError(Exception):
    """A fixed public error code, never an exception message or private path."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("ascii")


def _json_object(payload: bytes) -> dict:
    value = json.loads(
        payload.decode("utf-8"),
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("response exceeds configured limit")
    return value


def _upstream_address(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = 80 if parsed.port is None else parsed.port
    except ValueError as exc:
        raise ValueError("upstream must be a numeric loopback HTTP origin") from exc
    if (
        parsed.scheme != "http"
        or not address.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65535
    ):
        raise ValueError("upstream must be a numeric loopback HTTP origin")
    return str(address), port


def _identifier(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not IDENTIFIER.fullmatch(value)
        or value.endswith(".")
    ):
        raise ValueError("invalid catalog identifier")
    return value


def _reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _validate_report_identity(
    result: bytes, pair: tuple[str, str], extension: str
) -> None:
    if extension == "json":
        report = _json_object(result)
        if report.get("run_id") != pair[0] or report.get("device_id") != pair[1]:
            raise ValueError("report resource mismatch")
    else:
        lines = result.decode("utf-8").splitlines()
        if (
            f"| run_id | `{pair[0]}` |" not in lines
            or f"| device_id | `{pair[1]}` |" not in lines
        ):
            raise ValueError("report resource mismatch")


class ConsoleApplication:
    """Keep browser-selected resources within an explicit public catalog."""

    def __init__(
        self,
        catalog: Path,
        runs_root: Path,
        query_base_url: str,
        token_file: Path | None,
        *,
        timeout: float = 3.0,
        live_status_provider: Callable[[], Mapping[str, Any]] | None = None,
        credential_provider: Callable[[], str] | None = None,
        live_report_provider: Callable[[tuple[str, str], str], bytes] | None = None,
        live_control_provider: (
            Callable[[str, tuple[str, str] | None], None] | None
        ) = None,
        laboratory_provider: LaboratoryController | None = None,
        operations_provider: OperationsController | None = None,
    ) -> None:
        if live_status_provider is not None and not callable(live_status_provider):
            raise ValueError("live status provider must be callable")
        self.live_status_provider = live_status_provider
        if live_control_provider is not None and not callable(live_control_provider):
            raise ValueError("live control provider must be callable")
        self.live_control_provider = live_control_provider
        self.laboratory = laboratory_provider
        self.operations = operations_provider
        self._waveform_slots = threading.BoundedSemaphore(2)
        if credential_provider is not None and not callable(credential_provider):
            raise ValueError("credential provider must be callable")
        if token_file is None and credential_provider is None:
            raise ValueError("server-side credentials are required")
        self.credential_provider = credential_provider
        if live_report_provider is not None and not callable(live_report_provider):
            raise ValueError("live report provider must be callable")
        self.live_report_provider = live_report_provider
        self.upstream = _upstream_address(query_base_url)
        self.runs_root = runs_root.resolve(strict=True)
        if not self.runs_root.is_dir():
            raise ValueError("runs root must be a directory")
        rows = json.loads(_read_bounded(catalog, MAX_CATALOG_BYTES).decode("utf-8"))
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("catalog must be a bounded list")
        self.devices: list[dict] = []
        self.allowed: set[tuple[str, str]] = set()
        used_runs: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid catalog entry")
            run_id, device_id = _identifier(row.get("run_id")), _identifier(
                row.get("device_id")
            )
            if run_id in used_runs:
                raise ValueError("each catalog run must identify one device")
            used_runs.add(run_id)
            public = {"run_id": run_id, "device_id": device_id}
            for key in (
                "scenario",
                "scenario_id",
                "total_points",
                "sample_rate_hz",
                "start_time_utc",
            ):
                if key not in row:
                    continue
                item = row[key]
                if key in ("total_points", "sample_rate_hz"):
                    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                        raise ValueError("invalid public catalog number")
                elif (
                    not isinstance(item, str)
                    or len(item) > 256
                    or any(ord(c) < 32 for c in item)
                ):
                    raise ValueError("invalid public catalog text")
                public[key] = item
            self.devices.append(public)
            self.allowed.add((run_id, device_id))
        self.token_file, self.timeout = token_file, timeout
        self.asset_root = resources.files("g15.console").joinpath("static")
        self.notifications = None
        self.email_notifications = None

    def live_status(self) -> dict[str, Any]:
        """Project only bounded public capture counters from an embedded runtime."""
        if self.live_status_provider is None:
            raise ConsoleError(404, "not_found")
        try:
            source = self.live_status_provider()
            if source["schema_version"] != "1" or source["status"] not in {
                "starting",
                "running",
                "stopping",
                "stopped",
                "error",
                "limit_reached",
            }:
                raise ValueError("invalid live state")

            def counter(value):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("invalid live counter")
                return value

            def stamp(value, *, optional=False):
                if value is None and optional:
                    return None
                if not isinstance(value, str) or len(value) > 40:
                    raise ValueError("invalid live timestamp")
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if (
                    parsed.tzinfo is None
                    or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
                ):
                    raise ValueError("invalid live timestamp")
                return value

            interval = source["interval_seconds"]
            if (
                isinstance(interval, bool)
                or not isinstance(interval, (int, float))
                or not math.isfinite(interval)
                or not 0 < interval <= 60
            ):
                raise ValueError("invalid live interval")
            rows = source["devices"]
            if not isinstance(rows, list) or len(rows) != len(self.allowed):
                raise ValueError("invalid live devices")
            devices, seen = [], set()
            for row in rows:
                pair = (row["run_id"], row["device_id"])
                if pair not in self.allowed or pair in seen:
                    raise ValueError("invalid live resource")
                seen.add(pair)
                public = {"run_id": pair[0], "device_id": pair[1]}
                for name in (
                    "captured_samples",
                    "processed_samples",
                    "windows_written",
                    "alarms_written",
                    "queue_depth",
                ):
                    public[name] = counter(row[name])
                for name in ("last_capture_at_utc", "last_commit_at_utc"):
                    public[name] = stamp(row.get(name), optional=True)
                public["error"] = (
                    None
                    if row.get("error") is None
                    else "采集或分析失败，请查看本机运行日志。"
                )
                if "capture_paused" in row:
                    if type(row["capture_paused"]) is not bool:
                        raise ValueError("invalid capture control state")
                    public["capture_paused"] = row["capture_paused"]
                if "pending_samples" in row:
                    pending = counter(row["pending_samples"])
                    if (
                        pending
                        != public["captured_samples"] - public["processed_samples"]
                    ):
                        raise ValueError("invalid pending sample count")
                    public["pending_samples"] = pending
                devices.append(public)
            result = {
                "schema_version": "1",
                "status": source["status"],
                "session_id": _identifier(source["session_id"]),
                "started_at_utc": stamp(source["started_at_utc"]),
                "updated_at_utc": stamp(source["updated_at_utc"]),
                "sample_rate_hz": counter(source["sample_rate_hz"]),
                "window_points": counter(source["window_points"]),
                "interval_seconds": interval,
                "devices": devices,
                "error": (
                    None
                    if source.get("error") is None
                    else "采集或分析失败，请查看本机运行日志。"
                ),
            }
            if self.live_control_provider is not None:
                result["controls_available"] = True
            for name in ("backpressure_events", "late_cycles"):
                if name in source:
                    result[name] = counter(source[name])
            if "limit_reason" in source:
                reason = source["limit_reason"]
                if reason not in (None, "duration_limit", "disk_limit"):
                    raise ValueError("invalid live protection state")
                result["limit_reason"] = reason
            if "latency" in source:
                latency = source["latency"]
                count = counter(latency["recent_count"])
                seconds = latency["recent_p95_seconds"]
                if (
                    count > 2048
                    or (count == 0 and seconds is not None)
                    or (
                        count > 0
                        and (
                            isinstance(seconds, bool)
                            or not isinstance(seconds, (int, float))
                            or not math.isfinite(seconds)
                            or seconds < 0
                        )
                    )
                ):
                    raise ValueError("invalid live commit latency")
                result["latency"] = {
                    "recent_count": count,
                    "recent_p95_seconds": seconds,
                }
            return result
        except ConsoleError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise ConsoleError(503, "live_unavailable") from exc

    def live_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Control only registered simulated resources in the current session."""
        if self.live_control_provider is None or self.live_status_provider is None:
            raise ConsoleError(503, "live_control_unavailable")
        if (
            set(payload)
            not in (
                {"session_id", "action"},
                {"session_id", "action", "run_id", "device_id"},
            )
            or payload.get("action") not in ("pause", "resume")
            or not isinstance(payload.get("session_id"), str)
            or not payload["session_id"]
        ):
            raise ConsoleError(400, "invalid_request")
        current = self.live_status()
        if payload["session_id"] != current["session_id"]:
            raise ConsoleError(409, "live_session_changed")
        pair = self.resource(payload) if "run_id" in payload else None
        if current["status"] != "running":
            raise ConsoleError(409, "live_not_running")
        try:
            self.live_control_provider(payload["action"], pair)
        except RuntimeError as exc:
            raise ConsoleError(409, "live_not_running") from exc
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise ConsoleError(503, "live_control_unavailable") from exc
        return self.live_status()

    def resource(self, parameters: dict[str, str]) -> tuple[str, str]:
        """Resolve an exact run/device pair without accepting a path or URL."""
        pair = parameters.get("run_id", ""), parameters.get("device_id", "")
        if pair not in self.allowed:
            raise ConsoleError(404, "not_found")
        return pair

    def waveform(self, parameters: dict[str, str]) -> dict:
        """Read verified bounded samples only for a registered public resource."""
        if set(parameters) - {"run_id", "device_id", "start_seq", "count"}:
            raise ConsoleError(400, "invalid_request")
        pair = self.resource(parameters)
        sample_range = self.sample_range(parameters)
        status, payload = self.upstream_get(pair, {"page_size": "1"})
        if status != 200:
            code = (
                "waveform_access_denied"
                if status in (401, 403, 404)
                else "waveform_unavailable"
            )
            raise ConsoleError(status, code)
        body = _json_object(payload)
        if body.get("run_id") != pair[0] or body.get("device_id") != pair[1]:
            raise ConsoleError(502, "invalid_upstream_response")
        return self._read_samples(self.runs_root, *pair, **sample_range)

    def authorize_operation(self, pair: tuple[str, str]) -> None:
        """Require current query authorization before reading registered live data."""
        self.resource({"run_id": pair[0], "device_id": pair[1]})
        status, _payload = self.upstream_get(pair, {"page_size": "1"})
        if status != 200:
            raise ConsoleError(status, "workbench_access_denied")

    def workbench_read(self, path: str, parameters: dict[str, str]) -> dict:
        """Route bounded workbench reads through the explicit resource registry."""
        if self.operations is None:
            raise ConsoleError(503, "workbench_unavailable")
        if path == "/api/workbench":
            if parameters:
                raise ConsoleError(400, "invalid_request")
            return self.operations.overview()
        if path.endswith("/compare"):
            if set(parameters) != {"ids"}:
                raise ConsoleError(400, "invalid_request")
            identities = parameters["ids"].split(",")
            if not 1 <= len(identities) <= 4 or len(set(identities)) != len(identities):
                raise ConsoleError(400, "invalid_request")
            return self.operations.compare(identities)
        allowed = {"id", "start_seq", "count"} if path.endswith("/waveform") else {"id"}
        if "id" not in parameters or set(parameters) - allowed:
            raise ConsoleError(400, "invalid_request")
        identity = parameters["id"]
        if path.endswith("/waveform"):
            roots, run_id, device_id = self.operations.resolve_waveform(identity)
            return self._read_samples(
                roots, run_id, device_id, **self.sample_range(parameters)
            )
        if path.endswith("/dispositions"):
            return self.operations.dispositions(identity)
        return self.operations.detail(identity)

    def _read_samples(self, runs_root, run_id, device_id, **sample_range):
        if not self._waveform_slots.acquire(blocking=False):
            raise ConsoleError(503, "waveform_busy")
        try:
            return read_waveform(runs_root, run_id, device_id, **sample_range)
        finally:
            self._waveform_slots.release()

    @staticmethod
    def sample_range(parameters: dict[str, str]) -> dict:
        """Parse decimal sample bounds without accepting booleans or fractions."""
        selected = {}
        for key in ("start_seq", "count"):
            if key in parameters:
                if not re.fullmatch(r"[0-9]{1,16}", parameters[key]):
                    raise ConsoleError(400, "invalid_request")
                selected[key] = int(parameters[key])
        return selected

    def laboratory_read(self, path: str, parameters: dict[str, str]) -> dict:
        """Keep independent experiments outside the live resource catalog."""
        if self.laboratory is None:
            raise ConsoleError(503, "laboratory_unavailable")
        if path == "/api/laboratory":
            if parameters:
                raise ConsoleError(400, "invalid_request")
            return self.laboratory.overview()
        allowed = {"id"}
        if path.endswith("/waveform"):
            allowed |= {"start_seq", "count"}
        if set(parameters) - allowed or "id" not in parameters:
            raise ConsoleError(400, "invalid_request")
        experiment_id = _identifier(parameters["id"])
        if path.endswith("/waveform"):
            run_directory, device_id = self.laboratory.run_directory(experiment_id)
            return self._read_samples(
                run_directory.parent,
                run_directory.name,
                device_id,
                **self.sample_range(parameters),
            )
        return self.laboratory.detail(experiment_id)

    def upstream_get(
        self, pair: tuple[str, str], parameters: dict[str, str]
    ) -> tuple[int, bytes]:
        """Read the rotating server-side token and make one nonredirecting GET."""
        try:
            token = (
                self.credential_provider()
                if self.credential_provider is not None
                else _read_bounded(self.token_file, MAX_TOKEN_BYTES)
                .decode("ascii")
                .strip()
            )
            if not TOKEN.fullmatch(token):
                raise ValueError("invalid credential")
        except (OSError, UnicodeError, ValueError) as exc:
            raise ConsoleError(503, "upstream_unavailable") from exc
        target = f"/v1/runs/{quote(pair[0], safe='')}/devices/{quote(pair[1], safe='')}/health"
        target += "?" + urlencode(parameters)
        connection = http.client.HTTPConnection(*self.upstream, timeout=self.timeout)
        deadline = time.monotonic() + self.timeout
        expired = threading.Event()
        timer = None
        try:
            connection.connect()
            upstream_socket = connection.sock

            def expire() -> None:
                expired.set()
                try:
                    upstream_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConsoleError(503, "upstream_unavailable")
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            connection.request(
                "GET",
                target,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if response.status < 200 or 300 <= response.status < 400:
                raise ConsoleError(502, "invalid_upstream_response")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if expired.is_set():
                raise ConsoleError(503, "upstream_unavailable")
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ConsoleError(502, "invalid_upstream_response")
            try:
                body = _json_object(payload)
                if token in json.dumps(body, ensure_ascii=True):
                    raise ValueError("credential reflected by upstream")
                if response.status == 200:
                    if (
                        body.get("run_id") != pair[0]
                        or body.get("device_id") != pair[1]
                    ):
                        raise ValueError("upstream resource mismatch")
                else:
                    public_errors = {
                        "unauthorized",
                        "not_found",
                        "invalid_request",
                        "internal",
                        "method_not_allowed",
                        "overloaded",
                    }
                    if (
                        not 400 <= response.status <= 599
                        or set(body) - {"error", "request_id", "trace_id"}
                        or body.get("error") not in public_errors
                    ):
                        raise ValueError("unexpected upstream error envelope")
                    for key in ("request_id", "trace_id"):
                        if key in body and (
                            not isinstance(body[key], str)
                            or not re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", body[key])
                        ):
                            raise ValueError("invalid public request identifier")
            except (UnicodeError, ValueError, RecursionError, TypeError) as exc:
                raise ConsoleError(502, "invalid_upstream_response") from exc
            return response.status, payload
        except (OSError, http.client.HTTPException) as exc:
            raise ConsoleError(503, "upstream_unavailable") from exc
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

    def report(self, pair: tuple[str, str], extension: str) -> tuple[int, bytes]:
        """Authorize before reading only the selected run's fixed public report."""
        status, payload = self.upstream_get(pair, {"page_size": "1"})
        if status != 200:
            return status, payload
        body = _json_object(payload)
        if body.get("run_id") != pair[0] or body.get("device_id") != pair[1]:
            raise ConsoleError(502, "invalid_upstream_response")
        if self.live_report_provider is not None:
            try:
                result = self.live_report_provider(pair, extension)
                if not isinstance(result, bytes) or len(result) > MAX_RESPONSE_BYTES:
                    raise ValueError("invalid live report payload")
                _validate_report_identity(result, pair, extension)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                raise ConsoleError(502, "invalid_report") from exc
            return 200, result
        candidate = self.runs_root / pair[0] / "reports" / f"summary.{extension}"
        try:
            for component in (candidate.parent.parent, candidate.parent, candidate):
                if _reparse(component):
                    raise ConsoleError(404, "not_found")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(self.runs_root) or not resolved.is_file():
                raise ConsoleError(404, "not_found")
            before = resolved.stat()
            descriptor = os.open(
                resolved,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ConsoleError(404, "not_found")
                if (
                    any(
                        _reparse(p)
                        for p in (candidate.parent.parent, candidate.parent, candidate)
                    )
                    or candidate.resolve(strict=True) != resolved
                ):
                    raise ConsoleError(404, "not_found")
                result = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(result) > MAX_RESPONSE_BYTES:
                raise ConsoleError(502, "invalid_report")
            try:
                if extension == "json":
                    report = _json_object(result)
                    if (
                        report.get("run_id") != pair[0]
                        or report.get("device_id") != pair[1]
                    ):
                        raise ValueError("report resource mismatch")
                else:
                    lines = result.decode("utf-8").splitlines()
                    if (
                        f"| run_id | `{pair[0]}` |" not in lines
                        or f"| device_id | `{pair[1]}` |" not in lines
                    ):
                        raise ValueError("report resource mismatch")
            except (UnicodeError, ValueError, RecursionError) as exc:
                raise ConsoleError(502, "invalid_report") from exc
            return 200, result
        except ConsoleError:
            raise
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise ConsoleError(404, "not_found") from exc


class _HeadReader(io.RawIOBase):
    def __init__(self, connection: socket.socket, timeout: float) -> None:
        super().__init__()
        self.connection, self.deadline = connection, time.monotonic() + timeout
        self.remaining = MAX_HEAD_BYTES

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.remaining <= 0:
            raise TimeoutError("request head unavailable")
        self.connection.settimeout(remaining)
        count = self.connection.recv_into(memoryview(buffer)[: self.remaining])
        self.remaining -= count
        return count


class ConsoleHTTPServer(ThreadingHTTPServer):
    """Loopback listener with bounded workers, head sizes and socket deadlines."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(
        self,
        application: ConsoleApplication,
        host: str,
        port: int,
        *,
        max_workers: int = 16,
        request_timeout: float = 3.0,
    ) -> None:
        address = ipaddress.ip_address(host)
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not address.is_loopback
            or not 0 <= port <= 65535
        ):
            raise ValueError("console must bind a loopback address")
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or not 1 <= max_workers <= 64
            or isinstance(request_timeout, bool)
            or not isinstance(request_timeout, (int, float))
            or not 0 < request_timeout <= 10
        ):
            raise ValueError("invalid console limits")
        self.application, self.request_timeout = application, request_timeout
        self.worker_slots = threading.BoundedSemaphore(max_workers)
        self.address_family = (
            socket.AF_INET6 if address.version == 6 else socket.AF_INET
        )
        super().__init__((host, port), ConsoleRequestHandler)
        self.allowed_hosts = {str(address), "localhost"}

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.request_timeout)
        return connection, address

    def process_request(self, request, client_address):
        if not self.worker_slots.acquire(blocking=False):
            try:
                request.settimeout(min(self.request_timeout, 0.1))
                deadline, head = time.monotonic() + 0.1, bytearray()
                while (
                    b"\r\n\r\n" not in head
                    and len(head) < MAX_HEAD_BYTES
                    and time.monotonic() < deadline
                ):
                    part = request.recv(min(4096, MAX_HEAD_BYTES - len(head)))
                    if not part:
                        break
                    head.extend(part)
                payload = _json_bytes({"error": "unavailable"})
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\nCache-Control: no-store\r\nContent-Length: "
                    + str(len(payload)).encode("ascii")
                    + b"\r\n\r\n"
                    + payload
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.worker_slots.release()

    def handle_error(self, _request, _client_address):
        _LOGGER.error("console connection failed")


class ConsoleRequestHandler(BaseHTTPRequestHandler):
    """Expose only the catalog, health proxy, fixed reports and fixed assets."""

    protocol_version = "HTTP/1.1"
    server_version = "G15Console/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.rfile.close()
        self.rfile = io.BufferedReader(
            _HeadReader(self.connection, self.server.request_timeout)
        )

    def log_message(self, _format, *args):
        del args

    def send_error(self, code, message=None, explain=None):
        del message, explain
        self._send(code, _json_bytes({"error": "invalid_request"}))

    def handle_expect_100(self):
        self._send(400, _json_bytes({"error": "invalid_request"}))
        return False

    def do_GET(self):  # noqa: N802  # pylint: disable=invalid-name
        """Read an allowlisted resource without exposing server credentials."""
        self._handle()

    def do_HEAD(self):  # noqa: N802  # pylint: disable=invalid-name
        """Apply the same access checks while suppressing the response body."""
        self._handle()

    def do_POST(self):  # noqa: N802  # pylint: disable=invalid-name
        """Allow explicit same-origin notification or simulated capture actions."""
        if self.path not in (
            "/api/live/control",
            "/api/laboratory/experiments",
            "/api/workbench/dispositions",
            "/api/notifications/settings",
            "/api/notifications/test",
            "/api/email-notifications/settings",
            "/api/email-notifications/test",
            "/api/notifications/settings/delete",
            "/api/email-notifications/settings/delete",
            "/api/email-notifications/pending/cancel",
        ):
            self._not_allowed()
            return
        try:
            self.close_connection = True
            self._check_origin()
            action_header = {
                "/api/live/control": "live-control",
                "/api/laboratory/experiments": "laboratory",
                "/api/workbench/dispositions": "workbench",
            }.get(self.path, "notifications")
            if len(self.headers.get_all("Origin", [])) != 1 or self.headers.get_all(
                "X-G15-Console-Action", []
            ) != [action_header]:
                raise ConsoleError(403, "forbidden_origin")
            lengths = self.headers.get_all("Content-Length", [])
            content_types = self.headers.get_all("Content-Type", [])
            body_limit = 16384 if self.path == "/api/workbench/dispositions" else 4096
            if (
                self.headers.get_all("Transfer-Encoding", [])
                or self.headers.get_all("Expect", [])
                or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
                or not 0 < int(lengths[0]) <= body_limit
                or len(content_types) != 1
                or content_types[0].lower()
                not in ("application/json", "application/json; charset=utf-8")
            ):
                raise ConsoleError(400, "invalid_request")
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise ConsoleError(400, "invalid_request")

            def unique(values):
                result = {}
                for key, value in values:
                    if key in result:
                        raise ValueError("duplicate notification field")
                    result[key] = value
                return result

            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=unique,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(payload, dict):
                raise ConsoleError(400, "invalid_request")
            if self.path == "/api/live/control":
                result = self.server.application.live_control(payload)
                self._send(200, _json_bytes(result))
                return
            if self.path == "/api/laboratory/experiments":
                laboratory = self.server.application.laboratory
                if laboratory is None:
                    raise ConsoleError(503, "laboratory_unavailable")
                self._send(202, _json_bytes(laboratory.create(payload)))
                return
            if self.path == "/api/workbench/dispositions":
                operations = self.server.application.operations
                if operations is None:
                    raise ConsoleError(503, "workbench_unavailable")
                self._send(200, _json_bytes(operations.record_disposition(payload)))
                return
            control = (
                getattr(self.server.application, "email_notifications", None)
                if self.path.startswith("/api/email-notifications/")
                else self.server.application.notifications
            )
            if control is None:
                raise ConsoleError(503, "notifications_unavailable")
            if self.path.endswith("/settings/delete"):
                result, status = control.delete_settings(payload), 200
            elif self.path.endswith("/pending/cancel"):
                result, status = control.cancel_pending(payload), 200
            elif self.path.endswith("/settings"):
                result, status = control.configure(payload), 200
            else:
                if set(payload) != {"run_id", "device_id"}:
                    raise ConsoleError(400, "invalid_request")
                pair = self.server.application.resource(payload)
                result, status = control.test(pair), 202
            self._send(status, _json_bytes(result))
        except (
            ConsoleError,
            NotificationRequestError,
            LaboratoryRequestError,
            OperationsRequestError,
        ) as exc:
            self._send(
                exc.status,
                _json_bytes({"error": exc.code}),
                extra={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
            )
        except (ValueError, UnicodeError, RecursionError, TypeError):
            self._send(400, _json_bytes({"error": "invalid_request"}))
        except Exception:  # pylint: disable=broad-exception-caught
            self._send(500, _json_bytes({"error": "internal"}))

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._not_allowed
        raise AttributeError(name)

    def _not_allowed(self):
        try:
            self._check_origin()
        except (
            ConsoleError,
            LaboratoryRequestError,
            WaveformRequestError,
            OperationsRequestError,
        ) as exc:
            self._send(exc.status, _json_bytes({"error": exc.code}))
            return
        self._send(
            405,
            _json_bytes({"error": "method_not_allowed"}),
            extra={"Allow": "GET, HEAD"},
        )

    def _check_origin(self):
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        sites = self.headers.get_all("Sec-Fetch-Site", [])
        if (
            len(hosts) != 1
            or len(origins) > 1
            or len(sites) > 1
            or (sites and sites[0] not in ("same-origin", "none"))
        ):
            raise ConsoleError(403, "forbidden_origin")
        try:
            host = urlsplit("http://" + hosts[0])
            if (
                host.hostname not in self.server.allowed_hosts
                or (host.port or 80) != self.server.server_port
                or host.username is not None
                or host.password is not None
                or host.path
                or host.query
                or host.fragment
            ):
                raise ValueError()
            if origins:
                origin = urlsplit(origins[0])
                if (
                    origin.scheme != "http"
                    or origin.hostname != host.hostname
                    or (origin.port or 80) != self.server.server_port
                    or origin.username is not None
                    or origin.password is not None
                    or origin.path
                    or origin.query
                    or origin.fragment
                ):
                    raise ValueError()
        except ValueError as exc:
            raise ConsoleError(403, "forbidden_origin") from exc

    def _handle(self):
        try:
            self.close_connection = True
            self.connection.settimeout(self.server.request_timeout)
            self._check_origin()
            lengths = self.headers.get_all("Content-Length", [])
            if (
                self.headers.get_all("Transfer-Encoding", [])
                or self.headers.get_all("Expect", [])
                or len(lengths) > 1
                or (lengths and lengths[0].strip() != "0")
            ):
                raise ConsoleError(400, "invalid_request")
            if len(self.path.encode("utf-8")) > MAX_TARGET_BYTES:
                raise ConsoleError(414, "invalid_request")
            target = urlsplit(self.path)
            if (
                target.scheme
                or target.netloc
                or target.fragment
                or not target.path.startswith("/")
            ):
                raise ConsoleError(400, "invalid_request")
            values = parse_qs(
                target.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=5,
            )
            if any(len(v) != 1 or not v[0] for v in values.values()):
                raise ConsoleError(400, "invalid_request")
            parameters = {k: v[0] for k, v in values.items()}
            status, payload, content_type, extra = self._dispatch(
                target.path, parameters
            )
            self._send(status, payload, content_type, extra)
        except (
            ConsoleError,
            LaboratoryRequestError,
            WaveformRequestError,
            OperationsRequestError,
        ) as exc:
            self._send(
                exc.status,
                _json_bytes({"error": exc.code}),
                extra={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
            )
        except (ValueError, UnicodeError):
            self._send(400, _json_bytes({"error": "invalid_request"}))
        except Exception:  # pylint: disable=broad-exception-caught
            self._send(500, _json_bytes({"error": "internal"}))

    def _dispatch(self, path, parameters):
        application = self.server.application
        content_type, extra = "application/json; charset=utf-8", {}
        if path in ASSETS:
            if path == "/":
                if set(parameters) - {"device", "run", "view"} or parameters.get(
                    "view", "health"
                ) not in (
                    "health",
                    "alarms",
                    "maintenance",
                    "reports",
                    "notifications",
                ):
                    raise ConsoleError(400, "invalid_request")
            elif path in (
                "/screen",
                "/operations",
                "/diagnosis",
                "/simulation",
                "/laboratory",
                "/workbench",
            ):
                if set(parameters) - {"device", "run"}:
                    raise ConsoleError(400, "invalid_request")
            elif parameters:
                raise ConsoleError(400, "invalid_request")
            name, content_type = ASSETS[path]
            item = application.asset_root.joinpath(name)
            try:
                if isinstance(item, Path) and (
                    _reparse(application.asset_root)
                    or _reparse(item)
                    or not item.resolve(strict=True).is_relative_to(
                        application.asset_root.resolve(strict=True)
                    )
                ):
                    raise OSError()
                with item.open("rb") as stream:
                    payload = stream.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise OSError()
            except OSError as exc:
                raise ConsoleError(404, "not_found") from exc
            return 200, payload, content_type, extra
        if path in (
            "/api/notifications",
            "/api/notifications/preview",
            "/api/email-notifications",
            "/api/email-notifications/preview",
        ):
            control = (
                getattr(application, "email_notifications", None)
                if path.startswith("/api/email-notifications")
                else application.notifications
            )
            if control is None:
                raise ConsoleError(503, "notifications_unavailable")
            try:
                if path.endswith("/preview"):
                    if set(parameters) != {"run_id", "device_id"}:
                        raise ConsoleError(400, "invalid_request")
                    result = control.preview(application.resource(parameters))
                else:
                    if parameters:
                        raise ConsoleError(400, "invalid_request")
                    result = control.status()
            except NotificationRequestError as exc:
                raise ConsoleError(exc.status, exc.code) from exc
            return 200, _json_bytes(result), content_type, extra
        if path == "/api/waveform":
            return (
                200,
                _json_bytes(application.waveform(parameters)),
                content_type,
                extra,
            )
        if path in (
            "/api/workbench",
            "/api/workbench/run",
            "/api/workbench/compare",
            "/api/workbench/dispositions",
            "/api/workbench/waveform",
        ):
            return (
                200,
                _json_bytes(application.workbench_read(path, parameters)),
                content_type,
                extra,
            )
        if path in (
            "/api/laboratory",
            "/api/laboratory/experiment",
            "/api/laboratory/waveform",
        ):
            return (
                200,
                _json_bytes(application.laboratory_read(path, parameters)),
                content_type,
                extra,
            )
        if path == "/api/live":
            if parameters:
                raise ConsoleError(400, "invalid_request")
            return 200, _json_bytes(application.live_status()), content_type, extra
        if path == "/api/catalog":
            if parameters:
                raise ConsoleError(400, "invalid_request")
            return (
                200,
                _json_bytes({"devices": application.devices, "readonly": True}),
                content_type,
                extra,
            )
        allowed = {"run_id", "device_id"}
        if path == "/api/health":
            allowed |= {"page_size", "windows_cursor", "alarms_cursor"}
        elif path == "/api/report/download":
            allowed.add("format")
        elif path != "/api/report":
            raise ConsoleError(404, "not_found")
        if set(parameters) - allowed:
            raise ConsoleError(400, "invalid_request")
        pair = application.resource(parameters)
        if path == "/api/health":
            forwarded = {
                k: v for k, v in parameters.items() if k not in ("run_id", "device_id")
            }
            forwarded.setdefault("page_size", "100")
            status, payload = application.upstream_get(pair, forwarded)
        else:
            file_format = (
                parameters.get("format", "json")
                if path.endswith("/download")
                else "json"
            )
            if file_format not in ("json", "markdown"):
                raise ConsoleError(400, "invalid_request")
            extension = "md" if file_format == "markdown" else "json"
            status, payload = application.report(pair, extension)
            if status == 200 and path.endswith("/download"):
                content_type = (
                    "text/markdown; charset=utf-8"
                    if extension == "md"
                    else content_type
                )
                extra["Content-Disposition"] = (
                    f'attachment; filename="summary.{extension}"'
                )
        if status == 401:
            extra["WWW-Authenticate"] = "Bearer"
        return status, payload, content_type, extra

    def _send(
        self,
        status,
        payload,
        content_type="application/json; charset=utf-8",
        extra=None,
    ):
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except OSError:
            pass


def create_console_server(
    catalog: Path | str,
    runs_root: Path | str,
    query_base_url: str,
    token_file: Path | str | None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    max_workers: int = 16,
    request_timeout: float = 3.0,
    live_status_provider: Callable[[], Mapping[str, Any]] | None = None,
    credential_provider: Callable[[], str] | None = None,
    live_report_provider: Callable[[tuple[str, str], str], bytes] | None = None,
    live_control_provider: Callable[[str, tuple[str, str] | None], None] | None = None,
    laboratory_provider: LaboratoryController | None = None,
    operations_provider: OperationsController | None = None,
) -> ConsoleHTTPServer:
    """Create, but do not start, a read-only console on numeric loopback only."""
    application = ConsoleApplication(
        Path(catalog),
        Path(runs_root),
        query_base_url,
        Path(token_file) if token_file is not None else None,
        timeout=request_timeout,
        live_status_provider=live_status_provider,
        credential_provider=credential_provider,
        live_report_provider=live_report_provider,
        live_control_provider=live_control_provider,
        laboratory_provider=laboratory_provider,
        operations_provider=operations_provider,
    )
    return ConsoleHTTPServer(
        application,
        host,
        port,
        max_workers=max_workers,
        request_timeout=request_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    """Start a separate loopback console without changing the existing CLI/API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--query-base-url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18467)
    arguments = parser.parse_args(argv)
    try:
        server = create_console_server(
            arguments.catalog,
            arguments.runs_root,
            arguments.query_base_url,
            arguments.token_file,
            host=arguments.host,
            port=arguments.port,
        )
    except (OSError, ValueError, UnicodeError):
        parser.error("invalid console configuration")
    try:
        print(
            f"Read-only console: http://{arguments.host}:{server.server_port}",
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
