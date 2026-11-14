"""Bounded replay/comparison snapshots and an independent disposition ledger.

Analysis databases remain read-only. A disposition records a human's note; it
never resets an alarm, changes a score or asserts that maintenance was performed.
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import stat
import threading
from typing import Callable

from g15.live.runtime import _WriterLock
from g15.live.schema import load_live_session
from g15.notifications.advice import maintenance_advice
from g15.pipeline.schema import DataContractError, ManifestStatus, manifest_from_dict
from g15.storage.database import connect_readonly
from g15.storage.repository import StorageError, decode_features_json

LIMITS = {
    "max_compare": 4,
    "max_windows": 8192,
    "max_alarms": 4096,
    "max_dispositions": 10000,
    "max_note_chars": 2000,
    "max_operator_chars": 80,
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z")
_MAX_DATABASE_BYTES = 512 * 1024**2
_MAX_LEDGER_BYTES = 32 * 1024**2
_MAX_RESPONSE_BYTES = 7 * 1024**2
_CAPTURE_KEYS = frozenset(
    {
        "devices",
        "sample_rate_hz",
        "window_points",
        "interval_seconds",
        "queue_capacity",
        "max_duration_seconds",
        "max_disk_bytes",
        "seed",
        "cycle_windows",
        "backpressure_timeout_seconds",
    }
)


class OperationsRequestError(Exception):
    """Stable HTTP status/code without exposing paths or internal exceptions."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _bounded(value: dict) -> dict:
    if len(_json(value)) > _MAX_RESPONSE_BYTES:
        raise OperationsRequestError(413, "source_too_large")
    return value


def _safe_path(path: Path, *, required: bool = True) -> Path:
    """Reject reparse points in every existing ancestor before resolving."""
    path = path.absolute()
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if part == path and not required:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("reparse path is not supported")
    return path.resolve(strict=required)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _read_json(path: Path, limit: int = 1048576):
    path = _safe_path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("source file is not bounded")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("source file is too large")
    return json.loads(
        content,
        object_pairs_hook=_unique,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )


def _identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or value.endswith(".")
    ):
        raise ValueError("invalid identity")
    return value


def _position(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("invalid position")
    return value


def _finite(value: object, maximum: float = math.inf) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= maximum
    ):
        raise ValueError("invalid measurement")
    return float(value)


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("invalid timestamp")
    return value


def _user_text(value: object, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(char) < 32 and char not in "\n\t" for char in value)
    ):
        raise OperationsRequestError(400, "invalid_request")
    return value.strip()


def _alarm_key(run_id: str, device_id: str, rule_id: str, start_seq: int) -> str:
    return _digest([run_id, device_id, rule_id, start_seq])


class OperationsController:
    """Compose authorized public data and append-only local operator notes.

    ``authorize`` must validate current upstream permission before returning.
    History rows are trusted server registrations, never browser-supplied paths.
    Every run is revalidated against its manifest/descriptor on every read.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        runs_root: Path | str,
        catalog_provider: Callable,
        authorize: Callable,
        laboratory=None,
        live_status_provider: Callable | None = None,
        history_provider: Callable | None = None,
    ) -> None:
        self.root = _safe_path(Path(root), required=False)
        self.root.mkdir(exist_ok=True)
        self.runs_root = _safe_path(Path(runs_root))
        self.catalog_provider = catalog_provider
        self.authorize = authorize
        self.laboratory = laboratory
        self.live_status_provider = live_status_provider
        self.history_provider = history_provider
        self._lock = threading.RLock()
        self._closed = False
        self._writer = _WriterLock(self.root / ".operations-writer.lock")
        self._writer.acquire()
        self._database = self.root / "dispositions.sqlite"
        try:
            with closing(self._connect()) as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS disposition_events (
                      sequence INTEGER PRIMARY KEY,
                      source_id TEXT NOT NULL,
                      run_id TEXT NOT NULL,
                      device_id TEXT NOT NULL,
                      alarm_key TEXT NOT NULL,
                      request_id TEXT NOT NULL UNIQUE,
                      event_json TEXT NOT NULL,
                      event_hash TEXT NOT NULL UNIQUE
                    );
                    CREATE TRIGGER IF NOT EXISTS dispositions_no_update
                      BEFORE UPDATE ON disposition_events BEGIN
                      SELECT RAISE(ABORT, 'disposition ledger is append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS dispositions_no_delete
                      BEFORE DELETE ON disposition_events BEGIN
                      SELECT RAISE(ABORT, 'disposition ledger is append-only'); END;
                """)
                self._verify_ledger(conn)
                conn.commit()
        except Exception:
            self._writer.release()
            raise

    def _connect(self) -> sqlite3.Connection:
        for path in (
            self._database,
            self._database.with_name(self._database.name + "-wal"),
            self._database.with_name(self._database.name + "-shm"),
        ):
            if path.exists() or path.is_symlink():
                _safe_path(path)
                if path.stat().st_size > _MAX_LEDGER_BYTES:
                    raise OperationsRequestError(507, "operations_storage_limit")
        connection = sqlite3.connect(self._database, timeout=3)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _verify_ledger(connection: sqlite3.Connection) -> None:
        previous = None
        count = connection.execute(
            "SELECT COUNT(*) FROM disposition_events"
        ).fetchone()[0]
        if count > LIMITS["max_dispositions"]:
            raise ValueError("disposition limit exceeded")
        rows = connection.execute(
            "SELECT sequence,source_id,run_id,device_id,alarm_key,request_id,event_json,event_hash FROM disposition_events ORDER BY sequence"
        )
        for expected, row in enumerate(rows, 1):
            event = json.loads(row[6], object_pairs_hook=_unique)
            digest = event.pop("event_hash")
            if (
                row[0] != expected
                or event["sequence"] != expected
                or event["previous_hash"] != previous
                or digest != _digest(event)
                or row[7] != digest
                or tuple(row[1:6])
                != tuple(
                    event[key]
                    for key in (
                        "source_id",
                        "run_id",
                        "device_id",
                        "alarm_key",
                        "request_id",
                    )
                )
            ):
                raise ValueError("disposition integrity mismatch")
            previous = digest

    def _sources(self) -> dict[str, dict]:
        if self._closed:
            raise OperationsRequestError(503, "operations_unavailable")
        current = self.catalog_provider()
        if not isinstance(current, list) or len(current) > 1000:
            raise ValueError("catalog limit exceeded")
        sources = {}
        for row in current:
            run_id, device_id = _identifier(row["run_id"]), _identifier(
                row["device_id"]
            )
            identity = "live:" + run_id
            if identity in sources:
                raise ValueError("duplicate source")
            sources[identity] = {
                **row,
                "id": identity,
                "source_kind": "live",
                "run_dir": self.runs_root / run_id,
                "run_id": run_id,
                "device_id": device_id,
            }
        history = self.history_provider() if self.history_provider else []
        if not isinstance(history, list) or len(history) > 1000:
            raise ValueError("history limit exceeded")
        for row in history:
            run_id, device_id = _identifier(row["run_id"]), _identifier(
                row["device_id"]
            )
            identity = "live:" + run_id
            if identity not in sources:
                sources[identity] = {
                    **row,
                    "id": identity,
                    "source_kind": "archive",
                    "run_id": run_id,
                    "device_id": device_id,
                }
        if self.laboratory:
            for row in self.laboratory.overview()["experiments"]:
                identity = "experiment:" + row["id"]
                sources[identity] = {
                    "id": identity,
                    "source_kind": "experiment",
                    "experiment_id": row["id"],
                    **row,
                }
                sources[identity]["id"] = identity
        return sources

    @staticmethod
    def _public(source: dict) -> dict:
        return {
            "id": source["id"],
            "source_kind": source["source_kind"],
            "run_id": source.get("run_id"),
            "device_id": source["device_id"],
            "scenario": source.get(
                "scenario",
                source.get(
                    "scenario_id", source.get("parameters", {}).get("profile", "live")
                ),
            ),
            "created_at_utc": source.get(
                "created_at_utc",
                source.get("started_at_utc", source.get("start_time_utc")),
            ),
            "updated_at_utc": source.get("updated_at_utc"),
            "status": source.get("status", "available"),
            "experiment_id": source.get("experiment_id"),
            "session_id": source.get("session_id"),
        }

    def overview(self) -> dict:
        """Enumerate only current grants, registered archives and saved experiments."""
        return {
            "available": True,
            "runs": [self._public(row) for row in self._sources().values()],
            "limits": dict(LIMITS),
            "observed_at_utc": _now(),
        }

    def _resolve(self, identity: str) -> tuple[dict, Path, dict]:
        try:
            return self._resolve_source(identity)
        except (OSError, ValueError, KeyError, TypeError, DataContractError) as exc:
            raise OperationsRequestError(503, "invalid_source") from exc

    def _resolve_source(self, identity: str) -> tuple[dict, Path, dict]:
        if not isinstance(identity, str) or len(identity) > 150:
            raise OperationsRequestError(400, "invalid_request")
        source = self._sources().get(identity)
        if source is None:
            raise OperationsRequestError(404, "not_found")
        source = deepcopy(source)
        if source["source_kind"] == "experiment":
            detail = self.laboratory.detail(source["experiment_id"])
            directory, device_id = self.laboratory.run_directory(
                source["experiment_id"]
            )
            if device_id != source["device_id"]:
                raise ValueError("experiment identity mismatch")
            source["configuration"] = detail["configuration"]
            source["parameters"] = detail["experiment"]["parameters"]
        else:
            if source["source_kind"] == "live":
                self.authorize((source["run_id"], source["device_id"]))
            directory = Path(source["run_dir"])
        directory = _safe_path(directory)
        run_id, device_id = source["run_id"], source["device_id"]
        if directory.name != run_id:
            raise ValueError("run identity mismatch")
        live_path = directory / "live-session.json"
        if live_path.exists() or live_path.is_symlink():
            descriptor = load_live_session(directory, run_id, device_id)
            if source.get("session_id") not in (None, descriptor["session_id"]):
                raise ValueError("session identity mismatch")
            source.update(
                {
                    key: descriptor[key]
                    for key in (
                        "session_id",
                        "status",
                        "updated_at_utc",
                        "started_at_utc",
                    )
                }
            )
            if source["source_kind"] == "archive" and descriptor["status"] not in {
                "stopped",
                "error",
                "limit_reached",
            }:
                raise ValueError("archive is still active")
            metadata = {
                "rate": descriptor["sample_rate_hz"],
                "window_points": descriptor["window_points"],
                "high_water": descriptor["processed_samples"],
                "descriptor": descriptor,
            }
        else:
            manifest = manifest_from_dict(_read_json(directory / "manifest.json"))
            if (
                manifest.status != ManifestStatus.COMPLETE
                or manifest.run_id != run_id
                or manifest.device_id != device_id
            ):
                raise ValueError("manifest identity mismatch")
            metadata = {
                "rate": manifest.sample_rate_hz,
                "window_points": 256,
                "high_water": manifest.total_points,
                "descriptor": None,
                "manifest": manifest,
            }
        _safe_path(directory / "artifacts")
        database = _safe_path(directory / "artifacts" / "results.sqlite")
        if not database.is_file() or database.stat().st_size > _MAX_DATABASE_BYTES:
            raise ValueError("invalid analysis database")
        for suffix in ("-wal", "-shm"):
            sidecar = database.with_name(database.name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _safe_path(sidecar)
                if sidecar.stat().st_size > _MAX_DATABASE_BYTES:
                    raise ValueError("analysis sidecar exceeds limit")
        return source, directory, metadata

    def resolve_waveform(self, identity: str) -> tuple[Path, str, str]:
        """Authorize and resolve one exact source for the verified waveform reader."""
        source, directory, _metadata = self._resolve(identity)
        return directory.parent, source["run_id"], source["device_id"]

    @staticmethod
    def _windows(connection, pair, metadata):
        query = "FROM feature_windows WHERE run_id=? AND device_id=? AND end_seq_exclusive<=?"
        values = (*pair, metadata["high_water"])
        count = connection.execute("SELECT COUNT(*) " + query, values).fetchone()[0]
        if count > LIMITS["max_windows"]:
            raise OperationsRequestError(413, "source_too_large")
        if connection.execute(
            "SELECT 1 " + query + " AND LENGTH(features_json)>65536 LIMIT 1", values
        ).fetchone():
            raise ValueError("oversized feature record")
        windows, versions, rules = [], set(), set()
        rows = connection.execute(
            "SELECT start_seq,end_seq_exclusive,health_score,features_json "
            + query
            + " ORDER BY start_seq",
            values,
        )
        previous_end = None
        for start, end, score, encoded in rows:
            start, end = _position(start), _position(end)
            features = decode_features_json(encoded)
            if (
                end <= start
                or (previous_end is not None and start < previous_end)
                or features.get("sample_rate_hz") != metadata["rate"]
                or features.get("window_start_seq") != start
                or features.get("sample_count") != end - start
            ):
                raise ValueError("invalid feature geometry")
            versions.add(_identifier(features.get("scoring_version")))
            rules.add(_identifier(features.get("rule_id")))
            windows.append(
                {
                    "start_seq": start,
                    "end_seq_exclusive": end,
                    "health_score": _finite(score, 100),
                    "time_seconds": start / metadata["rate"],
                }
            )
            previous_end = end
        return windows, sorted(versions), sorted(rules)

    @staticmethod
    def _alarms(connection, pair, metadata):
        query = "FROM alarms WHERE run_id=? AND device_id=? AND start_seq<?"
        values = (*pair, metadata["high_water"])
        if (
            connection.execute("SELECT COUNT(*) " + query, values).fetchone()[0]
            > LIMITS["max_alarms"]
        ):
            raise OperationsRequestError(413, "source_too_large")
        if connection.execute(
            "SELECT 1 " + query + " AND LENGTH(reason)>4096 LIMIT 1", values
        ).fetchone():
            raise ValueError("oversized alarm record")
        alarms = []
        for start, rule, level, reason, stamp in connection.execute(
            "SELECT start_seq,rule_id,level,reason,created_at "
            + query
            + " ORDER BY start_seq,rule_id",
            values,
        ):
            start, rule = _position(start), _identifier(rule)
            if (
                level not in {"yellow", "red"}
                or not isinstance(reason, str)
                or not reason
            ):
                raise ValueError("invalid alarm")
            item = {
                "alarm_key": _alarm_key(*pair, rule, start),
                "start_seq": start,
                "rule_id": rule,
                "level": level,
                "reason": reason,
                "created_at": _timestamp(stamp),
                "time_seconds": start / metadata["rate"],
            }
            # The recommendation describes this historical event, not the device's
            # current active state and never a future event beyond a replay cursor.
            item["advice"] = list(
                maintenance_advice(
                    [item], [{"rule_id": rule, "state": {"active_level": level}}]
                )
            )
            alarms.append(item)
        return alarms

    def _latency(self, source):
        value = {
            "kind": "unavailable",
            "count": None,
            "mean_seconds": None,
            "max_seconds": None,
            "elapsed_seconds": None,
            "label": "未记录处理延迟",
        }
        if source["source_kind"] == "experiment":
            elapsed = source.get("elapsed_seconds")
            if elapsed is not None:
                value.update(
                    kind="experiment_elapsed",
                    elapsed_seconds=_finite(elapsed),
                    label="实验总耗时（生成、分析与报告），不是采样提交延迟",
                )
            return value
        observed = source.get("latency")
        if source["source_kind"] == "live" and self.live_status_provider:
            status = self.live_status_provider()
            row = next(
                (
                    item
                    for item in status.get("devices", [])
                    if item.get("run_id") == source["run_id"]
                    and item.get("device_id") == source["device_id"]
                ),
                None,
            )
            observed = row.get("latency") if row else None
        if observed and type(observed.get("count")) is int and observed["count"] > 0:
            value.update(
                kind="capture_to_commit",
                count=observed["count"],
                mean_seconds=_finite(observed["mean_seconds"]),
                max_seconds=_finite(observed["max_seconds"]),
                label="样本生成开始至 SQLite 提交返回（全程均值／最大值）",
            )
        return value

    def _parameters(self, source, directory):
        if "parameters" in source:
            return deepcopy(source["parameters"])
        config_path = directory.parent.parent / "capture-config.json"
        if config_path.exists():
            config = _read_json(config_path, 131072).get("config", {})
            return {
                key: value
                for key, value in config.items()
                if key in _CAPTURE_KEYS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            }
        return {}

    def detail(self, identity: str) -> dict:
        """Read complete bounded health/alarms from one committed SQLite snapshot."""
        try:
            source, directory, metadata = self._resolve(identity)
            pair = source["run_id"], source["device_id"]
            with closing(
                connect_readonly(directory / "artifacts" / "results.sqlite")
            ) as conn:
                conn.execute("BEGIN")
                row = conn.execute(
                    "SELECT schema_version,scoring_version,status,scenario_id,started_at FROM runs WHERE run_id=?",
                    (pair[0],),
                ).fetchone()
                descriptor = metadata["descriptor"]
                if row is None or (descriptor is None and row[2] != "complete"):
                    raise ValueError("missing run identity")
                if descriptor and tuple(row) != (
                    descriptor["schema_version"],
                    descriptor["scoring_version"],
                    "streaming",
                    descriptor["scenario_id"],
                    descriptor["started_at_utc"],
                ):
                    raise ValueError("stored run identity mismatch")
                if not descriptor:
                    manifest = metadata["manifest"]
                    if (row[0], row[3], row[4]) != (
                        manifest.schema_version,
                        manifest.scenario_id,
                        manifest.start_time_utc,
                    ):
                        raise ValueError("stored manifest identity mismatch")
                windows, versions, rules = self._windows(conn, pair, metadata)
                alarms = self._alarms(conn, pair, metadata)
                conn.commit()
            source_windows = {item["start_seq"]: item for item in windows}
            for alarm in alarms:
                window = source_windows.get(alarm["start_seq"])
                if window is None:
                    raise ValueError("alarm source window missing")
                alarm["end_seq_exclusive"] = window["end_seq_exclusive"]
                alarm["available_seq"] = window["end_seq_exclusive"] - 1
            scores = [item["health_score"] for item in windows]
            return _bounded(
                {
                    "run": self._public(source),
                    "configuration": {
                        **source.get("configuration", {}),
                        "parameters": self._parameters(source, directory),
                        "sample_rate_hz": metadata["rate"],
                        "window_points": metadata["window_points"],
                        "scoring_versions": versions,
                        "rule_ids": rules,
                    },
                    "windows": windows,
                    "alarms": alarms,
                    "available_points": metadata["high_water"],
                    "summary": {
                        "window_count": len(windows),
                        "alarm_count": len(alarms),
                        "red_count": sum(item["level"] == "red" for item in alarms),
                        "yellow_count": sum(
                            item["level"] == "yellow" for item in alarms
                        ),
                        "health_min": min(scores) if scores else None,
                        "health_max": max(scores) if scores else None,
                        "health_latest": scores[-1] if scores else None,
                        "processed_points": sum(
                            item["end_seq_exclusive"] - item["start_seq"]
                            for item in windows
                        ),
                    },
                    "latency": self._latency(source),
                    "dispositions": self._events(pair),
                    "complete": True,
                    "limits": dict(LIMITS),
                    "observed_at_utc": _now(),
                }
            )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            sqlite3.Error,
            StorageError,
            DataContractError,
        ) as exc:
            raise OperationsRequestError(503, "invalid_source") from exc

    def compare(self, identities: list[str]) -> dict:
        """Compare up to four complete runs aligned by elapsed sample time."""
        if (
            not isinstance(identities, list)
            or not 1 <= len(identities) <= LIMITS["max_compare"]
            or any(not isinstance(item, str) for item in identities)
            or len(set(identities)) != len(identities)
        ):
            raise OperationsRequestError(400, "invalid_request")
        runs = []
        for identity in identities:
            detail = self.detail(identity)
            runs.append(
                {
                    key: value
                    for key, value in detail.items()
                    if key not in {"alarms", "dispositions", "limits"}
                }
            )
        return _bounded(
            {
                "runs": runs,
                "alignment": "elapsed_seconds",
                "generated_at_utc": _now(),
                "limits": dict(LIMITS),
            }
        )

    def _events(self, pair: tuple[str, str]) -> list[dict]:
        with self._lock, closing(self._connect()) as conn:
            size = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(event_json)),0) FROM disposition_events WHERE run_id=? AND device_id=?",
                pair,
            ).fetchone()[0]
            if size > _MAX_RESPONSE_BYTES:
                raise OperationsRequestError(413, "source_too_large")
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT event_json FROM disposition_events WHERE run_id=? AND device_id=? ORDER BY sequence",
                    pair,
                )
            ]

    def dispositions(self, identity: str) -> dict:
        """Read the durable trail only after reauthorizing its source."""
        source, _directory, _metadata = self._resolve(identity)
        pair = source["run_id"], source["device_id"]
        return _bounded(
            {"run_id": pair[0], "device_id": pair[1], "events": self._events(pair)}
        )

    def record_disposition(self, payload: dict) -> dict:
        """Append an explicit acknowledgment or note; request IDs are idempotent."""
        fields = {"id", "alarm_key", "action", "operator", "note", "request_id"}
        if not isinstance(payload, dict) or set(payload) != fields:
            raise OperationsRequestError(400, "invalid_request")
        if (
            payload["action"] not in ("acknowledge", "note")
            or not isinstance(payload["alarm_key"], str)
            or not _DIGEST.fullmatch(payload["alarm_key"])
            or not isinstance(payload["request_id"], str)
            or not _REQUEST_ID.fullmatch(payload["request_id"])
        ):
            raise OperationsRequestError(400, "invalid_request")
        operator = _user_text(payload["operator"], LIMITS["max_operator_chars"])
        note = _user_text(payload["note"], LIMITS["max_note_chars"])
        detail = self.detail(payload["id"])
        alarm = next(
            (
                row
                for row in detail["alarms"]
                if row["alarm_key"] == payload["alarm_key"]
            ),
            None,
        )
        if alarm is None:
            raise OperationsRequestError(404, "not_found")
        source = detail["run"]
        body = {
            "source_id": source["id"],
            "run_id": source["run_id"],
            "device_id": source["device_id"],
            "alarm_key": alarm["alarm_key"],
            "rule_id": alarm["rule_id"],
            "start_seq": alarm["start_seq"],
            "action": payload["action"],
            "operator": operator,
            "note": note,
            "request_id": payload["request_id"],
        }
        with self._lock:
            if self._closed:
                raise OperationsRequestError(503, "operations_unavailable")
            return self._append_disposition(body)

    def _append_disposition(self, body: dict) -> dict:
        """Append while the caller retains the writer lifetime lock."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute(
                "SELECT event_json FROM disposition_events WHERE request_id=?",
                (body["request_id"],),
            ).fetchone()
            if old:
                event = json.loads(old[0])
                if any(event[key] != value for key, value in body.items()):
                    raise OperationsRequestError(409, "request_conflict")
                return {"event": event, "duplicate": True}
            count = conn.execute("SELECT COUNT(*) FROM disposition_events").fetchone()[
                0
            ]
            if count >= LIMITS["max_dispositions"]:
                raise OperationsRequestError(429, "operations_full")
            if body["action"] == "acknowledge":
                old_events = conn.execute(
                    "SELECT event_json FROM disposition_events WHERE alarm_key=?",
                    (body["alarm_key"],),
                )
                if any(
                    json.loads(row[0])["action"] == "acknowledge" for row in old_events
                ):
                    raise OperationsRequestError(409, "already_acknowledged")
            previous = conn.execute(
                "SELECT sequence,event_hash FROM disposition_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            event = {
                **body,
                "sequence": previous[0] + 1 if previous else 1,
                "created_at_utc": _now(),
                "previous_hash": previous[1] if previous else None,
            }
            event["event_hash"] = _digest(event)
            conn.execute(
                "INSERT INTO disposition_events VALUES(?,?,?,?,?,?,?,?)",
                (
                    event["sequence"],
                    event["source_id"],
                    event["run_id"],
                    event["device_id"],
                    event["alarm_key"],
                    event["request_id"],
                    _json(event),
                    event["event_hash"],
                ),
            )
            conn.commit()
            return {"event": event, "duplicate": False}

    def close(self) -> None:
        """Release the local ledger writer after in-flight requests complete."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._writer.release()
