"""Explicit bounded registration of completed local simulator sessions."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import math
from pathlib import Path
import re
import stat

_LOG = logging.getLogger(__name__)
_SESSION = re.compile(r"live-[0-9]{8}T[0-9]{6}-[a-f0-9]{10}\Z")
_FINISHED = frozenset({"stopped", "limit_reached", "error"})
_CONFIG_FIELDS = (
    "sample_rate_hz",
    "window_points",
    "interval_seconds",
    "seed",
    "cycle_windows",
    "devices",
)


def _regular(path: Path, *, directory: bool = False) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        return False
    return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)


def _read(path: Path, limit: int = 131072):
    if not _regular(path) or path.stat().st_size > limit:
        raise ValueError("invalid archived session file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("archived session file too large")
    return json.loads(data)


def _positive_integer(value):
    return type(value) is int and 0 < value <= 1_000_000


class LiveHistoryRegistry:
    """Freeze up to 64 finished sessions from an explicitly supplied local root.

    This is a local console resource grant, independent of the current session's
    rotating query token. Browser input never supplies a path or adds a grant.
    Files are revalidated by readers before accessing analysis or raw samples.
    """

    def __init__(self, root: Path, *, current_session: str):
        root = Path(root)
        if not _regular(root, directory=True):
            raise ValueError("invalid live history root")
        self.root = root.resolve(strict=True)
        self._rows = []
        self.skipped_sessions = 0
        directories = []
        for index, path in enumerate(self.root.iterdir()):
            if index >= 4096:
                raise ValueError("live history directory limit exceeded")
            if _SESSION.fullmatch(path.name) and path.name != current_session:
                directories.append(path)
        for path in sorted(directories, reverse=True)[:64]:
            try:
                rows = self._session(path)
                if len(self._rows) + len(rows) > 1000:
                    raise ValueError("archive resource limit exceeded")
                self._rows.extend(rows)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                RecursionError,
                OverflowError,
            ):
                self.skipped_sessions += 1
                _LOG.warning("An invalid or unfinished archived session was omitted")

    def _session(self, directory: Path) -> list[dict]:
        if not _regular(directory, directory=True):
            raise ValueError("invalid archived session directory")
        status_path = directory / "session-status.json"
        status = _read(status_path)
        if (
            not isinstance(status, dict)
            or status.get("session_id") != directory.name
            or status.get("status") not in _FINISHED
        ):
            raise ValueError("session is not a finished local archive")
        catalog = _read(directory / "catalog.json")
        capture = _read(directory / "capture-config.json")
        if not isinstance(catalog, list) or not 1 <= len(catalog) <= 100:
            raise ValueError("invalid archived catalog")
        if not isinstance(capture, dict) or not isinstance(capture.get("config"), dict):
            raise ValueError("invalid archived configuration")
        configuration = {key: capture["config"].get(key) for key in _CONFIG_FIELDS}
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 2**64 - 1
            or not math.isfinite(value)
            for value in configuration.values()
        ):
            raise ValueError("invalid archived public configuration")
        runs_root = directory / "runs"
        if not _regular(runs_root, directory=True):
            raise ValueError("invalid archived runs directory")
        devices = status.get("devices")
        if not isinstance(devices, list) or len(devices) != len(catalog):
            raise ValueError("archived catalog/status mismatch")
        result, seen = [], set()
        for row in catalog:
            run_id, device_id = row["run_id"], row["device_id"]
            if (
                not isinstance(run_id, str)
                or not re.fullmatch(re.escape(directory.name) + r"-d[0-9]{2,3}", run_id)
                or not isinstance(device_id, str)
                or not re.fullmatch(r"LIVE-[0-9]{3}", device_id)
                or run_id in seen
            ):
                raise ValueError("invalid archived resource identity")
            seen.add(run_id)
            run_dir = runs_root / run_id
            if not _regular(run_dir, directory=True):
                raise ValueError("invalid archived run directory")
            descriptor = _read(run_dir / "live-session.json")
            matching = [
                item
                for item in devices
                if isinstance(item, dict)
                and item.get("run_id") == run_id
                and item.get("device_id") == device_id
            ]
            if (
                len(matching) != 1
                or not isinstance(descriptor, dict)
                or descriptor.get("run_id") != run_id
                or descriptor.get("device_id") != device_id
                or descriptor.get("session_id") != directory.name
                or descriptor.get("status") not in _FINISHED
                or not _positive_integer(descriptor.get("sample_rate_hz"))
                or not _positive_integer(descriptor.get("window_points"))
            ):
                raise ValueError("archived resource identity mismatch")
            result.append(
                {
                    "run_id": run_id,
                    "device_id": device_id,
                    "run_dir": run_dir,
                    "source_kind": "archive",
                    "session_id": directory.name,
                    "started_at_utc": descriptor.get("started_at_utc"),
                    "sample_rate_hz": descriptor["sample_rate_hz"],
                    "window_points": descriptor["window_points"],
                    "status": descriptor["status"],
                    "latency": deepcopy(matching[0].get("latency")),
                    "parameters": configuration,
                }
            )
        return result

    def __call__(self) -> list[dict]:
        """Return the exact registered server-owned descriptors, never paths from UI."""
        return deepcopy(self._rows)
