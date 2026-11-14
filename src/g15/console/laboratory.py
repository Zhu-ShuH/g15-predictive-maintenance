"""Bounded independent simulation experiments using the existing pipeline.

Experiments never join the live device catalog. Only the reporting layer reads
fault truth; the laboratory exposes stored analysis and a report projection.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import uuid

import yaml

from g15.config import ConsumerConfig, load_config, validate_config
from g15.live.runtime import _WriterLock
from g15.live.schema import atomic_json
from g15.pipeline.consumer import consume_run
from g15.pipeline.producer import produce_run_detailed
from g15.reporting.builder import build_report
from g15.simulation.faults import FAULT_TYPES
from g15.storage.database import connect_readonly

LIMITS = {
    "max_concurrent": 1,
    "max_experiments": 20,
    "max_disk_bytes": 128 * 1024**2,
    "min_points": 8192,
    "max_points": 65536,
    "point_step": 256,
    "sample_rate_hz": 256,
    "window_points": 256,
    "fault_types": list(FAULT_TYPES),
    "fault_min_duration_points": {"spike": 1, "step": 1, "burst": 3},
}
PRESETS = [
    dict(
        profile="normal",
        seed=7,
        total_points=8192,
        noise_std=0.05,
        degradation_rate_per_sample=0.0,
        fault_amplitude=0.0,
    ),
    dict(
        profile="degradation",
        seed=42,
        total_points=16384,
        noise_std=0.05,
        degradation_rate_per_sample=0.00015,
        fault_amplitude=0.0,
    ),
    dict(
        profile="sudden_fault",
        seed=42,
        total_points=8192,
        noise_std=0.05,
        degradation_rate_per_sample=0.0,
        fault_amplitude=6.0,
    ),
]
_FIELDS = frozenset(PRESETS[0])
_FAULT_FIELDS = frozenset({"fault_type", "fault_start_seq", "fault_duration_points"})
_ID = re.compile(r"lab-[a-f0-9]{16}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ACTIVE = frozenset({"queued", "generating", "analysing", "reporting"})
_STATES = _ACTIVE | {"completed", "failed"}
_RESERVE_BYTES = 8 * 1024**2
_SUMMARY_FIELDS = (
    "scenario_kind",
    "verifiable",
    "passed",
    "total_points",
    "processed_points",
    "analysed_points",
    "feature_windows",
    "alarm_count",
    "health_score_min",
    "health_score_max",
    "fault_events",
    "detected_events",
    "missed_events",
    "detection_rate",
    "false_alarm_count",
    "elapsed_seconds",
    "scoring_version",
)


class LaboratoryRequestError(Exception):
    """A stable HTTP status/code; never disclose internal exception text."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _parameters(value: object) -> dict:
    if not isinstance(value, dict) or set(value) not in (
        _FIELDS,
        _FIELDS | _FAULT_FIELDS,
    ):
        raise LaboratoryRequestError(400, "invalid_request")
    result = dict(value)
    if result["profile"] not in ("normal", "degradation", "sudden_fault"):
        raise LaboratoryRequestError(400, "invalid_request")
    for name, low, high in (("seed", 0, 2**31 - 1), ("total_points", 8192, 65536)):
        item = result[name]
        if type(item) is not int or not low <= item <= high:
            raise LaboratoryRequestError(400, "invalid_request")
    if result["total_points"] % LIMITS["point_step"]:
        raise LaboratoryRequestError(400, "invalid_request")
    for name, high in (
        ("noise_std", 1.0),
        ("degradation_rate_per_sample", 0.001),
        ("fault_amplitude", 10.0),
    ):
        item = result[name]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            or not 0 <= item <= high
        ):
            raise LaboratoryRequestError(400, "invalid_request")
        result[name] = float(item)
    if (
        result["profile"] != "degradation" and result["degradation_rate_per_sample"]
    ) or (result["profile"] != "sudden_fault" and result["fault_amplitude"]):
        raise LaboratoryRequestError(400, "invalid_request")
    if _FAULT_FIELDS <= result.keys():
        if (
            result["profile"] != "sudden_fault"
            or result["fault_type"] not in FAULT_TYPES
        ):
            raise LaboratoryRequestError(400, "invalid_request")
        start, duration = result["fault_start_seq"], result["fault_duration_points"]
        minimum = 3 if result["fault_type"] == "burst" else 1
        if (
            type(start) is not int
            or type(duration) is not int
            or start < 0
            or duration < minimum
            or start + duration > result["total_points"]
        ):
            raise LaboratoryRequestError(400, "invalid_request")
    return result


def _fault_event(parameters: dict) -> dict | None:
    """Resolve a public injection plan without rewriting legacy parameters."""
    if parameters["profile"] != "sudden_fault":
        return None
    return {
        "event_id": "LAB-F001",
        "type": parameters.get("fault_type", "spike"),
        "start_seq": parameters.get(
            "fault_start_seq", (parameters["total_points"] // 2 // 256) * 256
        ),
        "duration_points": parameters.get("fault_duration_points", 512),
        "amplitude": parameters["fault_amplitude"],
    }


def _json_read(path: Path, limit: int = 2 * 1024**2) -> dict:
    if _reparse(path) or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("invalid laboratory record")
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("laboratory record too large")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("invalid laboratory record")
    return value


def _config(parameters: dict, device_id: str) -> dict:
    profile = parameters["profile"]
    scenario = {
        "id": "laboratory_" + profile,
        "description": "Independent bounded laboratory experiment",
        "seed": parameters["seed"],
        # Fixed simulated epoch makes a repeated seed/config reproducible;
        # actual execution timestamps live in the experiment record.
        "start_time_utc": "2026-01-01T00:00:00Z",
        "device_id": device_id,
        "sample_rate_hz": 256,
        "total_points": parameters["total_points"],
        "chunk_points": 4096,
    }
    if profile != "sudden_fault":
        scenario["kind"] = profile
    event = _fault_event(parameters)
    events = [event] if event is not None else []
    return {
        "schema_version": "1.0",
        "scenario": scenario,
        "signals": {
            "vibration": {
                "baseline": 0.5,
                "noise_std": parameters["noise_std"],
                "degradation_rate_per_sample": parameters[
                    "degradation_rate_per_sample"
                ],
                "unit": "g",
            },
            "temperature": {
                "baseline": 35.0,
                "noise_std": 0.15,
                "degradation_rate_per_sample": 0.0,
                "unit": "degC",
            },
        },
        "fault_generation": {
            "mode": "manual",
            "stochastic_probability_per_window": 0.0,
            "events": events,
        },
    }


def _finite(value, low=0, high=math.inf):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and low <= value <= high
    )


def _validate_results(value: dict, record: dict) -> None:
    """Reject changed identities, impossible values and unbounded public data."""
    expected = {
        "run_id",
        "device_id",
        "summary",
        "windows",
        "alarms",
        "alarm_limit",
        "alarms_truncated",
        "sample_rate_hz",
        "window_points",
    }
    if (
        set(value) != expected
        or value["run_id"] != record["run_id"]
        or value["device_id"] != record["device_id"]
        or value["sample_rate_hz"] != 256
        or value["window_points"] != 256
        or value["alarm_limit"] != 512
        or type(value["alarms_truncated"]) is not bool
    ):
        raise ValueError("invalid laboratory result identity")
    summary = value["summary"]
    if not isinstance(summary, dict) or set(summary) != set(_SUMMARY_FIELDS):
        raise ValueError("invalid laboratory summary fields")
    points = record["parameters"]["total_points"]
    kind = record["parameters"]["profile"]
    if (
        summary["total_points"] != points
        or summary["processed_points"] != points
        or summary["analysed_points"] != points
        or summary["scenario_kind"] != ("fault" if kind == "sudden_fault" else kind)
        or type(summary["verifiable"]) is not bool
        or (summary["verifiable"] and type(summary["passed"]) is not bool)
        or (not summary["verifiable"] and summary["passed"] is not None)
    ):
        raise ValueError("invalid laboratory summary identity")
    for key in (
        "total_points",
        "processed_points",
        "analysed_points",
        "feature_windows",
        "alarm_count",
        "fault_events",
        "detected_events",
        "missed_events",
        "false_alarm_count",
    ):
        item = summary[key]
        if item is not None and (type(item) is not int or item < 0):
            raise ValueError("invalid laboratory summary count")
    for key, high in (
        ("health_score_min", 100),
        ("health_score_max", 100),
        ("detection_rate", 1),
        ("elapsed_seconds", math.inf),
    ):
        if summary[key] is not None and not _finite(summary[key], high=high):
            raise ValueError("invalid laboratory summary measure")
    version = summary["scoring_version"]
    if not isinstance(version, str) or not _RUN_ID.fullmatch(version):
        raise ValueError("invalid laboratory scoring version")
    windows, alarms = value["windows"], value["alarms"]
    if (
        not isinstance(windows, list)
        or len(windows) != points // 256
        or summary["feature_windows"] != len(windows)
        or not isinstance(alarms, list)
        or len(alarms) > 512
    ):
        raise ValueError("invalid laboratory result count")
    for index, row in enumerate(windows):
        if (
            not isinstance(row, dict)
            or set(row) != {"start_seq", "end_seq_exclusive", "health_score"}
            or type(row["start_seq"]) is not int
            or row["start_seq"] != index * 256
            or type(row["end_seq_exclusive"]) is not int
            or row["end_seq_exclusive"] != (index + 1) * 256
            or not _finite(row["health_score"], high=100)
        ):
            raise ValueError("invalid laboratory window")
    scores = [row["health_score"] for row in windows]
    if summary["health_score_min"] != min(scores) or summary["health_score_max"] != max(
        scores
    ):
        raise ValueError("inconsistent laboratory health summary")
    for row in alarms:
        if (
            not isinstance(row, dict)
            or set(row) != {"start_seq", "rule_id", "level", "reason", "created_at"}
            or type(row["start_seq"]) is not int
            or not 0 <= row["start_seq"] < points
            or row["start_seq"] % 256
            or row["level"] not in ("red", "yellow")
            or not isinstance(row["rule_id"], str)
            or not _RUN_ID.fullmatch(row["rule_id"])
            or not isinstance(row["reason"], str)
            or len(row["reason"]) > 4096
            or not isinstance(row["created_at"], str)
            or len(row["created_at"]) > 40
        ):
            raise ValueError("invalid laboratory alarm")
        stamp = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        if stamp.utcoffset() != timezone.utc.utcoffset(stamp):
            raise ValueError("invalid laboratory alarm timestamp")


class LaboratoryController:
    """One asynchronous worker, finite history and durable independent runs."""

    def __init__(self, root: Path | str, health_rules_path: Path | str):
        self.root = Path(root)
        if self.root.exists() and _reparse(self.root):
            raise ValueError("laboratory root cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve(strict=True)
        self.health_rules_path = Path(health_rules_path).resolve(strict=True)
        self._lock = threading.RLock()
        self._writer_lock = _WriterLock(self.root / ".laboratory-writer.lock")
        self._writer_lock.acquire()
        self._records = {}
        self._closed = False
        self._active_id = None
        try:
            self._restore()
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="g15-lab"
            )
        except Exception:
            self._writer_lock.release()
            raise

    def _restore(self):
        directories = sorted(self.root.glob("lab-*"))
        if len(directories) > LIMITS["max_experiments"]:
            raise ValueError("laboratory history exceeds limit")
        for directory in directories:
            if (
                not _ID.fullmatch(directory.name)
                or _reparse(directory)
                or not directory.is_dir()
            ):
                raise ValueError("invalid laboratory history directory")
            record = _json_read(directory / "experiment.json", 32768)
            expected = {
                "id",
                "status",
                "created_at_utc",
                "updated_at_utc",
                "parameters",
                "run_id",
                "device_id",
                "error",
                "elapsed_seconds",
            }
            if set(record) != expected or record["id"] != directory.name:
                raise ValueError("invalid laboratory identity")
            record["parameters"] = _parameters(record["parameters"])
            if record["status"] not in _STATES:
                raise ValueError("invalid laboratory status")
            if record["device_id"] != "LAB-" + directory.name[4:12].upper():
                raise ValueError("invalid laboratory device")
            for key in ("created_at_utc", "updated_at_utc"):
                stamp = datetime.fromisoformat(record[key].replace("Z", "+00:00"))
                if stamp.utcoffset() != timezone.utc.utcoffset(stamp):
                    raise ValueError("invalid laboratory timestamp")
            run_id = record["run_id"]
            if run_id is not None and (
                not isinstance(run_id, str)
                or not _RUN_ID.fullmatch(run_id)
                or run_id.endswith(".")
            ):
                raise ValueError("invalid laboratory run")
            if record["error"] not in (
                None,
                "experiment_failed",
                "storage_limit",
                "interrupted",
            ):
                raise ValueError("invalid laboratory error")
            elapsed = record["elapsed_seconds"]
            if elapsed is not None and (
                isinstance(elapsed, bool)
                or not isinstance(elapsed, (int, float))
                or not math.isfinite(elapsed)
                or elapsed < 0
            ):
                raise ValueError("invalid laboratory duration")
            self._records[record["id"]] = record
            if record["status"] == "completed":
                if record["error"] is not None or record["elapsed_seconds"] is None:
                    raise ValueError("inconsistent completed laboratory record")
                self.run_directory(record["id"])
                _validate_results(_json_read(directory / "results.json"), record)
            if record["status"] in _ACTIVE:
                record.update(
                    status="failed", error="interrupted", updated_at_utc=_utc_now()
                )
                self._persist(record)

    def _persist(self, record):
        atomic_json(self.root / record["id"] / "experiment.json", record)

    def _disk_bytes(self):
        size = 0
        for path in self.root.rglob("*"):
            if _reparse(path):
                raise LaboratoryRequestError(503, "laboratory_unavailable")
            if path.is_file():
                size += path.stat().st_size
        return size

    def overview(self) -> dict:
        """Return public limits, parameter presets and newest-first history."""
        with self._lock:
            records = sorted(
                self._records.values(),
                key=lambda row: row["created_at_utc"],
                reverse=True,
            )
            return {
                "available": not self._closed,
                "limits": deepcopy(LIMITS),
                "presets": deepcopy(PRESETS),
                "experiments": deepcopy(records),
            }

    def create(self, payload: dict) -> dict:
        """Accept one bounded independent job; reject overload without queuing."""
        parameters = _parameters(payload)
        with self._lock:
            if self._closed:
                raise LaboratoryRequestError(503, "laboratory_unavailable")
            if self._active_id is not None:
                raise LaboratoryRequestError(409, "laboratory_busy")
            if len(self._records) >= LIMITS["max_experiments"]:
                raise LaboratoryRequestError(429, "laboratory_full")
            if (
                self._disk_bytes() + _RESERVE_BYTES > LIMITS["max_disk_bytes"]
                or shutil.disk_usage(self.root).free < _RESERVE_BYTES + 16 * 1024**2
            ):
                raise LaboratoryRequestError(507, "laboratory_storage_limit")
            identity = "lab-" + uuid.uuid4().hex[:16]
            directory = self.root / identity
            directory.mkdir()
            stamp = _utc_now()
            record = {
                "id": identity,
                "status": "queued",
                "created_at_utc": stamp,
                "updated_at_utc": stamp,
                "parameters": parameters,
                "run_id": None,
                "device_id": "LAB-" + identity[4:12].upper(),
                "error": None,
                "elapsed_seconds": None,
            }
            self._persist(record)
            self._records[identity] = record
            self._active_id = identity
            self._executor.submit(self._execute, identity)
            return {"experiment": deepcopy(record)}

    def _record(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise LaboratoryRequestError(404, "not_found")
        record = self._records.get(identity)
        if record is None:
            raise LaboratoryRequestError(404, "not_found")
        return record

    def _change(self, identity, **values):
        with self._lock:
            record = self._records[identity]
            record.update(values, updated_at_utc=_utc_now())
            self._persist(record)

    def _check_budget(self, identity):
        directory = self.root / identity
        if (
            sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
            > _RESERVE_BYTES
            or self._disk_bytes() > LIMITS["max_disk_bytes"]
        ):
            raise LaboratoryRequestError(507, "laboratory_storage_limit")

    def _execute(self, identity):
        began = time.perf_counter()
        try:
            self._change(identity, status="generating")
            with self._lock:
                record = deepcopy(self._records[identity])
            directory = self.root / identity
            parameters = record["parameters"]
            config_data = _config(parameters, record["device_id"])
            config_path = directory / "scenario.yaml"
            config_path.write_text(
                yaml.safe_dump(config_data, allow_unicode=False, sort_keys=False),
                encoding="utf-8",
            )
            atomic_json(directory / "parameters.json", parameters)
            rules_path = directory / "health-rules.yaml"
            with self.health_rules_path.open("rb") as stream:
                rules_bytes = stream.read(131073)
            if len(rules_bytes) > 131072:
                raise ValueError("health rule snapshot exceeds limit")
            rules_path.write_bytes(rules_bytes)
            producer_config = load_config(config_path)
            consumer_config = ConsumerConfig(
                "1.0", 256, "drop", "health-rules.yaml", rules_path
            )
            validate_config(producer_config, consumer=consumer_config)
            production = produce_run_detailed(producer_config, directory / "runs")
            self._check_budget(identity)
            run_dir = production.run_dir
            self._change(identity, status="analysing", run_id=run_dir.name)
            database = run_dir / "artifacts" / "results.sqlite"
            database.parent.mkdir(exist_ok=True)
            consume_run(run_dir, database, consumer_config)
            self._check_budget(identity)
            self._change(identity, status="reporting")
            # The builder is the only component here allowed to load fault truth.
            report_path = build_report(run_dir, database)
            report = _json_read(report_path)
            results = self._results(database, run_dir.name, record["device_id"], report)
            _validate_results(results, {**record, "run_id": run_dir.name})
            atomic_json(directory / "results.json", results)
            self._check_budget(identity)
            self._change(
                identity,
                status="completed",
                elapsed_seconds=time.perf_counter() - began,
            )
        except Exception as exc:  # A failed experiment must not stop the live runtime.
            code = (
                "storage_limit"
                if isinstance(exc, LaboratoryRequestError)
                and getattr(exc, "code", None) == "laboratory_storage_limit"
                else "experiment_failed"
            )
            try:
                self._change(
                    identity,
                    status="failed",
                    error=code,
                    elapsed_seconds=time.perf_counter() - began,
                )
            except OSError:
                # In-memory failure remains truthful even when disk is full.
                with self._lock:
                    self._records[identity].update(status="failed", error=code)
        finally:
            with self._lock:
                self._active_id = None
                if self._closed:
                    self._writer_lock.release()

    @staticmethod
    def _results(database, run_id, device_id, report):
        if report.get("run_id") != run_id or report.get("device_id") != device_id:
            raise ValueError("report identity mismatch")
        with closing(connect_readonly(database)) as conn:
            conn.execute("BEGIN")
            windows = [
                dict(zip(("start_seq", "end_seq_exclusive", "health_score"), row))
                for row in conn.execute(
                    "SELECT start_seq,end_seq_exclusive,health_score "
                    "FROM feature_windows WHERE run_id=? AND device_id=? "
                    "ORDER BY start_seq LIMIT 257",
                    (run_id, device_id),
                )
            ]
            alarms = [
                dict(
                    zip(("start_seq", "rule_id", "level", "reason", "created_at"), row)
                )
                for row in conn.execute(
                    "SELECT start_seq,rule_id,level,reason,created_at "
                    "FROM alarms WHERE run_id=? AND device_id=? "
                    "ORDER BY start_seq,rule_id LIMIT 513",
                    (run_id, device_id),
                )
            ]
        if len(windows) > 256:
            raise ValueError("experiment window limit exceeded")
        return {
            "run_id": run_id,
            "device_id": device_id,
            "summary": {key: report.get(key) for key in _SUMMARY_FIELDS},
            "windows": windows,
            "alarms": alarms[:512],
            "alarm_limit": 512,
            "alarms_truncated": len(alarms) > 512,
            "sample_rate_hz": 256,
            "window_points": 256,
        }

    def run_directory(self, identity: str) -> tuple[Path, str]:
        """Resolve a completed experiment for the bounded raw waveform reader."""
        with self._lock:
            record = self._record(identity)
            if record["status"] != "completed" or record["run_id"] is None:
                raise LaboratoryRequestError(409, "experiment_not_ready")
            run_dir = self.root / identity / "runs" / record["run_id"]
            if (
                not run_dir.is_dir()
                or any(
                    _reparse(part)
                    for part in (self.root / identity, run_dir.parent, run_dir)
                )
                or not run_dir.resolve().is_relative_to(
                    (self.root / identity).resolve()
                )
            ):
                raise LaboratoryRequestError(503, "laboratory_unavailable")
            return run_dir, record["device_id"]

    def detail(self, identity: str) -> dict:
        """Return persisted experiment parameters and completed real analysis."""
        with self._lock:
            record = deepcopy(self._record(identity))
        results = None
        if record["status"] == "completed":
            self.run_directory(identity)
            try:
                results = _json_read(self.root / identity / "results.json")
                _validate_results(results, record)
            except (OSError, ValueError, TypeError) as exc:
                raise LaboratoryRequestError(503, "laboratory_unavailable") from exc
        parameters = record["parameters"]
        event = _fault_event(parameters)
        configuration = {
            "sample_rate_hz": 256,
            "window_points": 256,
            "chunk_points": 4096,
            "simulated_start_time_utc": "2026-01-01T00:00:00Z",
            "fault_type": event["type"] if event else None,
            "fault_start_seq": event["start_seq"] if event else None,
            "fault_duration_points": event["duration_points"] if event else 0,
            "fault_amplitude": event["amplitude"] if event else 0,
            "fault_end_seq_exclusive": (
                event["start_seq"] + event["duration_points"] if event else None
            ),
        }
        return {
            "experiment": record,
            "configuration": configuration,
            "results": results,
        }

    def close(self, wait: bool = True) -> None:
        """Reject new jobs and finish accepted work without deleting evidence."""
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)
        with self._lock:
            if self._active_id is None:
                self._writer_lock.release()
