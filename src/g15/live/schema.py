"""Strict, separate live-session descriptors; never an incomplete Manifest."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

SCHEMA_VERSION = "1"
SCENARIO_ID = "live-wall-clock-v1"
STATES = frozenset(
    {"starting", "running", "stopping", "stopped", "error", "limit_reached"}
)
FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "run_id",
        "device_id",
        "scenario_id",
        "source_kind",
        "sample_rate_hz",
        "window_points",
        "interval_seconds",
        "started_at_utc",
        "updated_at_utc",
        "status",
        "captured_samples",
        "processed_samples",
        "windows_written",
        "alarms_written",
        "scoring_version",
        "rule_id",
        "error",
    }
)
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def utc_now() -> str:
    """Return an actual UTC wall-clock observation, with explicit timezone."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: object) -> None:
    """Replace a complete JSON document atomically, preserving the old on error."""
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    atomic_replace(temporary, path)


def atomic_replace(temporary: Path, target: Path) -> None:
    """Replace atomically; bound Windows sharing-violation waits from readers."""
    deadline = time.monotonic() + 1.0
    while True:
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate live descriptor key")
        value[key] = item
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("live timestamp must be UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("live timestamp must be UTC")
    return parsed


class _DescriptorChanged(ValueError):
    """An atomic replacement raced this attempt; retry from the full checks."""


def load_live_session(
    run_dir: Path | str, expected_run_id: str, expected_device_id: str
) -> dict[str, object]:
    """Validate one bounded public descriptor; malformed input raises ValueError."""
    deadline = time.monotonic() + 1.0
    while True:
        try:
            return _load_live_session(run_dir, expected_run_id, expected_device_id)
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.005)
        except _DescriptorChanged:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _load_live_session(run_dir, expected_run_id, expected_device_id):
    directory = Path(run_dir)
    manifest = directory / "manifest.json"
    path = directory / "live-session.json"
    if manifest.exists() or manifest.is_symlink():
        raise ValueError("manifest/live ambiguity")
    for item, required_type in ((directory, stat.S_ISDIR), (path, stat.S_ISREG)):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("live reparse paths are not supported")
        if not required_type(info.st_mode):
            raise ValueError("live path has unsupported file type")
    if not path.resolve(strict=True).is_relative_to(directory.resolve(strict=True)):
        raise ValueError("live descriptor escaped run")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("live path has unsupported file type")
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise _DescriptorChanged("live descriptor changed during open")
        payload = stream.read(65537)
    if len(payload) > 65536:
        raise ValueError("live descriptor too large")
    value = json.loads(payload, object_pairs_hook=_object)
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("invalid live descriptor fields")
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["scenario_id"] != SCENARIO_ID
        or value["source_kind"] != "wall_clock_simulator"
    ):
        raise ValueError("unsupported live descriptor")
    for key in ("session_id", "run_id", "device_id", "scoring_version", "rule_id"):
        if (
            not isinstance(value[key], str)
            or not ID.fullmatch(value[key])
            or value[key].endswith(".")
        ):
            raise ValueError("invalid live identifier")
    if (
        value["run_id"] != expected_run_id
        or value["device_id"] != expected_device_id
        or directory.name != expected_run_id
    ):
        raise ValueError("live identity mismatch")
    if not isinstance(value["status"], str) or value["status"] not in STATES:
        raise ValueError("invalid live status")
    for key in (
        "sample_rate_hz",
        "window_points",
        "captured_samples",
        "processed_samples",
        "windows_written",
        "alarms_written",
    ):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("invalid live integer")
    if (
        not 1 <= value["sample_rate_hz"] <= 4096
        or not 2 <= value["window_points"] <= 4096
    ):
        raise ValueError("invalid live geometry")
    interval = value["interval_seconds"]
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (float, int))
        or not math.isfinite(interval)
        or not 0.02 <= interval <= 5
    ):
        raise ValueError("invalid live interval")
    samples = value["sample_rate_hz"] * interval
    if abs(samples - round(samples)) > 1e-8 or round(samples) % value["window_points"]:
        raise ValueError("live intervals must contain complete windows")
    if (
        value["processed_samples"] > value["captured_samples"]
        or value["windows_written"] * value["window_points"]
        != value["processed_samples"]
        or value["captured_samples"] % value["window_points"]
        or value["alarms_written"] > value["windows_written"]
    ):
        raise ValueError("live counters do not reconcile")
    if _timestamp(value["updated_at_utc"]) < _timestamp(value["started_at_utc"]):
        raise ValueError("live timestamps regressed")
    if value["error"] is not None and (
        not isinstance(value["error"], str)
        or not re.fullmatch(r"[a-z_]{1,64}", value["error"])
    ):
        raise ValueError("invalid live error code")
    return value
