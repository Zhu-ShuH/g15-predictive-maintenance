"""Bounded process-crash evidence, without IPC or durable writes during HTTP.

One wave owns one exclusive file-backed mapping. A frame is visible to recovery
only after its length, sequence, SHA and JSON have been written and its commit
marker is written last. This protects against a killed *process*, while its OS
and page cache survive. It is not a power-loss or torn-sector guarantee.

Normal append uses no file write, pipe, flush, fsync or worker thread. A proven
upper bound permits C JSON encoding for small frames with at most 64 KiB scratch;
larger frames stream small chunks into the mapping. No wave of Python objects
or unbounded full-frame buffer is retained. Input objects belong to callers;
callers must not mutate them concurrently with append.
"""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import re
import struct
import threading
from typing import Any

MAX_BYTES = 16 * 1024 * 1024
_SMALL_FRAME_BYTES = 64 * 1024
_HEADER = struct.Struct(">8sHHQ64s")
_MAGIC = b"G15EVSP1"
_HEADER_COMMIT = b"HDRDONE!"
_HEADER_SIZE = _HEADER.size + 32 + len(_HEADER_COMMIT)
_FRAME = struct.Struct(">4sIQ32s")
_FRAME_MAGIC = b"EVT1"
_COMMIT = b"EVTDONE!"
_SEAL = struct.Struct(">8sQQ32s8s")
_SEAL_MAGIC = b"G15SEAL1"
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SAFE_KIND = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SECRET_KEYS = frozenset(
    {
        "authorization",
        "token",
        "access_token",
        "bearer_token",
        "bearer_key",
        "cursor_key",
        "cursor",
        "windows_cursor",
        "alarms_cursor",
        "headers",
        "payload",
        "raw_body",
        "response_bytes_base64",
        "private_truth",
    }
)


class SpoolError(RuntimeError):
    """A sticky evidence failure; preserve files and fail the enclosing wave."""


def _safe_name(value: str) -> bool:
    return type(value) is str and _SAFE_NAME.fullmatch(value) is not None


def _validate(value: Any, active: set[int] | None = None, depth: int = 0) -> int:
    """Validate once and bound ensure_ascii JSON scratch before C encoding."""
    if depth > 64:
        raise ValueError("JSON nesting limit")
    if value is None or type(value) is bool:
        return 5
    if type(value) is str:
        return 12 * len(value) + 2
    if type(value) is int:
        return value.bit_length() * 30103 // 100000 + 3
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite JSON number")
        return 64
    if type(value) not in (dict, list):
        raise TypeError("non JSON type")
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise ValueError("cyclic JSON")
    active.add(identity)
    bound = 2 + max(0, len(value) - 1)
    try:
        entries = value.items() if type(value) is dict else enumerate(value)
        for key, item in entries:
            if type(value) is dict:
                if type(key) is not str:
                    raise TypeError("non string JSON key")
                if key.lower() in _SECRET_KEYS:
                    raise ValueError("forbidden evidence field")
                bound += 12 * len(key) + 3
            bound += _validate(item, active, depth + 1)
    finally:
        active.remove(identity)
    return bound


def _chunks(value: Any) -> Iterator[bytes]:
    """Encode strict JSON with bounded scalar chunks and no full-frame copy."""
    if type(value) is str:
        yield b'"'
        for start in range(0, len(value), 1024):
            yield json.dumps(value[start : start + 1024], ensure_ascii=True)[
                1:-1
            ].encode("ascii")
        yield b'"'
    elif type(value) is dict:
        yield b"{"
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield b","
            yield from _chunks(key)
            yield b":"
            yield from _chunks(item)
        yield b"}"
    elif type(value) is list:
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _chunks(item)
        yield b"]"
    else:
        yield json.dumps(value, allow_nan=False, ensure_ascii=True).encode("ascii")


def _projection(value: Any, active: set[int] | None = None, depth: int = 0) -> Any:
    """Failure-only safe projection; never repr an unknown object or secret."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if depth > 64 or type(value) not in (dict, list):
        return {"unserializable_type": type(value).__name__}
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        return {"unserializable_type": "cycle"}
    active.add(identity)
    try:
        if type(value) is list:
            return [_projection(item, active, depth + 1) for item in value]
        return {
            key if type(key) is str else "<non-string-key>": (
                "[redacted]"
                if type(key) is str and key.lower() in _SECRET_KEYS
                else _projection(item, active, depth + 1)
            )
            for key, item in value.items()
        }
    finally:
        active.remove(identity)


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_load(payload: bytes) -> Any:
    def pairs(values):
        output = {}
        for key, value in values:
            if key in output:
                raise ValueError("duplicate JSON key")
            output[key] = value
        return output

    def bad_constant(_value):
        raise ValueError("nonfinite JSON constant")

    return json.loads(payload, object_pairs_hook=pairs, parse_constant=bad_constant)


class WaveEventSpool:
    """Exclusive per-wave mmap with sticky failure and streamed emergency backup.

    append returns None on success. On failure it first attempts to retain the
    rejected known envelope, then raises SpoolError. Every later append is also
    retained in the emergency stream and raises; no reset can clear the failure.
    seal flushes/fsyncs only after HTTP. A failed seal preserves the allocated
    file, including a possibly partial frame, and raises. close never seals.
    """

    def __init__(
        self, directory: Path | str, wave_id: str, max_bytes: int = MAX_BYTES
    ) -> None:
        if not _safe_name(wave_id):
            raise ValueError("invalid spool wave identity")
        if type(max_bytes) is not int or not 512 <= max_bytes <= MAX_BYTES:
            raise ValueError("invalid spool byte capacity")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.wave_id, self.max_bytes = wave_id, max_bytes
        self.path = directory / f"{wave_id}.events.spool"
        self.emergency_path = directory / f"{wave_id}.events.emergency.ndjson"
        if self.emergency_path.exists():
            raise FileExistsError("prior emergency evidence exists")
        self._lock = threading.RLock()
        self._attempts = self._committed = self._emergency_committed = 0
        self._errors: list[str] = []
        self._error_count = 0
        self._sealed = self._closed = self._fsynced = False
        self._retention_incomplete = False
        self._sha256: str | None = None
        self._emergency = None
        self._emergency_identity = None
        self._emergency_size = 0
        self._emergency_needs_separator = False
        self._mapping = None
        self._file = self.path.open("x+b", buffering=0)
        try:
            self._file.truncate(max_bytes)
            self._mapping = mmap.mmap(self._file.fileno(), max_bytes)
            wave = wave_id.encode("ascii")
            prefix = _HEADER.pack(
                _MAGIC, 1, len(wave), max_bytes, wave.ljust(64, b"\0")
            )
            header = prefix + hashlib.sha256(prefix).digest() + _HEADER_COMMIT
            self._mapping[:_HEADER_SIZE] = header
            self._used = _HEADER_SIZE
            self._chain = hashlib.sha256(header)
        except BaseException:
            if self._mapping is not None:
                self._mapping.close()
            self._file.close()
            raise

    def _error(self, code: str) -> None:
        self._error_count += 1
        if len(self._errors) < 64:
            self._errors.append(code)

    def _put(self, offset: int, payload: bytes) -> None:
        self._mapping[offset : offset + len(payload)] = payload

    def _append_frame(self, sequence: int, message: dict, bound: int) -> None:
        offset = self._used
        self._put(offset, _FRAME.pack(_FRAME_MAGIC, 0, sequence, b"\0" * 32))
        position = offset + _FRAME.size
        digest = hashlib.sha256(struct.pack(">Q", sequence))
        blocks = (
            (
                json.dumps(
                    message, ensure_ascii=True, allow_nan=False, separators=(",", ":")
                ).encode("ascii"),
            )
            if bound <= _SMALL_FRAME_BYTES
            else _chunks(message)
        )
        for block in blocks:
            if position + len(block) + len(_COMMIT) + _SEAL.size > self.max_bytes:
                raise OverflowError("spool byte capacity exceeded")
            self._put(position, block)
            position += len(block)
            digest.update(block)
        length = position - offset - _FRAME.size
        self._put(offset, _FRAME.pack(_FRAME_MAGIC, length, sequence, digest.digest()))
        self._put(position, _COMMIT)  # Last mutation makes this frame committed.
        self._used = position + len(_COMMIT)
        self._committed += 1
        for start in range(offset, self._used, 4096):
            self._chain.update(self._mapping[start : min(start + 4096, self._used)])

    def _write_emergency(
        self, sequence: int, message: dict, reason: str, complete: bool
    ) -> None:
        try:
            if self._emergency is None:
                if self._emergency_identity is None:
                    self._emergency = self.emergency_path.open("xb", buffering=0)
                    info = os.fstat(self._emergency.fileno())
                    self._emergency_identity = (info.st_dev, info.st_ino)
                else:
                    fd = os.open(
                        self.emergency_path,
                        os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        info = os.fstat(fd)
                        if (
                            (info.st_dev, info.st_ino) != self._emergency_identity
                            or info.st_size != self._emergency_size
                        ):
                            raise OSError("owned emergency evidence changed")
                        self._emergency = os.fdopen(fd, "ab", buffering=0)
                    except BaseException:
                        os.close(fd)
                        raise
            if self._emergency_needs_separator:
                self._emergency_write(b"\n")
                self._emergency_needs_separator = False
            prefix = {
                "format": "g15-wave-event-spool-emergency-v1",
                "wave_id": self.wave_id,
                "sequence": sequence,
                "reason": reason,
                "serialization_complete": complete,
            }
            for block in _chunks(prefix):
                if block == b"}":
                    self._emergency_write(b',"message":')
                else:
                    self._emergency_write(block)
            digest = hashlib.sha256()
            for block in _chunks(message):
                self._emergency_write(block)
                digest.update(block)
            self._emergency_write(
                b',"message_sha256":"' + digest.hexdigest().encode() + b'"}\n'
            )
            self._emergency_committed += 1
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            self._retention_incomplete = True
            self._emergency_needs_separator = True
            self._error("emergency_" + type(exc).__name__)
            if not isinstance(exc, Exception):
                raise

    def _emergency_write(self, payload: bytes) -> None:
        while payload:
            count = self._emergency.write(payload)
            if not count:
                raise OSError("emergency write made no progress")
            payload = payload[count:]

    def append(self, kind: str, event: dict) -> None:
        """Freeze original metadata; never replace origin timestamps or PID."""
        with self._lock:
            sequence = self._attempts
            self._attempts += 1
            message = {"kind": kind, "event": event, "wave_id": self.wave_id}
            complete = True
            interrupt = None
            try:
                if type(kind) is not str or _SAFE_KIND.fullmatch(kind) is None:
                    raise ValueError("invalid event kind")
                if type(event) is not dict:
                    raise TypeError("event must be an object")
                bound = _validate(message)
            except BaseException as exc:  # pylint: disable=broad-exception-caught
                complete = False
                self._error("serialization_" + type(exc).__name__)
                if not isinstance(exc, Exception):
                    interrupt = exc
                message = {
                    "kind": (
                        kind
                        if type(kind) is str and _SAFE_KIND.fullmatch(kind)
                        else "spool_rejected"
                    ),
                    "event": _projection(event),
                    "wave_id": self.wave_id,
                    "spool_rejection": type(exc).__name__,
                }
            if not self._errors and not self._sealed and not self._closed:
                try:
                    self._append_frame(sequence, message, bound)
                    return
                except BaseException as exc:  # pylint: disable=broad-exception-caught
                    self._error("append_" + type(exc).__name__)
                    self._write_emergency(sequence, message, self._errors[-1], complete)
                    if not isinstance(exc, Exception):
                        raise
                    raise SpoolError("event spool append failed") from exc
            if not self._errors:
                self._error(
                    "append_after_seal" if self._sealed else "append_after_close"
                )
            self._write_emergency(sequence, message, self._errors[0], complete)
            if interrupt is not None:
                raise interrupt
            raise SpoolError("event spool is failed")

    def _close_handles(self) -> None:
        for attr in ("_mapping", "_file", "_emergency"):
            handle = getattr(self, attr)
            if handle is not None:
                try:
                    if attr == "_emergency":
                        self._emergency_size = os.fstat(handle.fileno()).st_size
                    handle.close()
                except BaseException as exc:  # pylint: disable=broad-exception-caught
                    self._error("close_" + type(exc).__name__)
                    if not isinstance(exc, Exception):
                        raise
                finally:
                    setattr(self, attr, None)
        self._closed = True

    def seal(self) -> dict:
        """Persist after HTTP and return a hash receipt; failures remain sticky."""
        with self._lock:
            if self._sealed and not self._errors:
                return self.facts()
            if self._closed and not self._errors:
                self._error("seal_after_close")
            try:
                if not self._errors:
                    footer = _SEAL.pack(
                        _SEAL_MAGIC,
                        self._committed,
                        self._used,
                        self._chain.digest(),
                        _COMMIT,
                    )
                    self._put(self._used, footer)
                    self._used += len(footer)
                if self._mapping is not None:
                    self._mapping.flush()
                if self._file is not None:
                    os.fsync(self._file.fileno())
                if self._emergency is not None:
                    self._emergency.flush()
                    os.fsync(self._emergency.fileno())
                if not self._errors:
                    self._mapping.close()
                    self._mapping = None
                    self._file.truncate(self._used)
                    os.fsync(self._file.fileno())
                    self._sealed = self._fsynced = True
            except BaseException as exc:  # pylint: disable=broad-exception-caught
                self._error("seal_" + type(exc).__name__)
                if not isinstance(exc, Exception):
                    self._close_handles()
                    raise
            self._close_handles()
            try:
                self._sha256 = _sha_file(self.path)
            except OSError as exc:
                self._error("hash_" + type(exc).__name__)
            if self._errors:
                raise SpoolError("event spool seal failed")
            return self.facts()

    def close(self) -> None:
        """Close without sealing or claiming fsync; preserve preseal bytes."""
        with self._lock:
            self._close_handles()
            if self._errors:
                raise SpoolError("event spool close failed")

    def facts(self) -> dict:
        """Safe counts and actual file sizes, including failure-path overflow."""
        with self._lock:
            emergency = None
            if self._emergency_identity is not None or self.emergency_path.exists():
                emergency = {
                    "path": self.emergency_path.name,
                    "bytes": None,
                    "sha256": None,
                    "committed_messages": self._emergency_committed,
                }
                try:
                    emergency["bytes"] = self.emergency_path.stat().st_size
                    emergency["sha256"] = _sha_file(self.emergency_path)
                except OSError as exc:
                    self._error("emergency_facts_" + type(exc).__name__)
            size = None
            try:
                size = self.path.stat().st_size
            except OSError as exc:
                self._error("spool_facts_" + type(exc).__name__)
            return {
                "format": "g15-wave-event-spool-v1",
                "wave_id": self.wave_id,
                "path": self.path.name,
                "bytes": size,
                "used_bytes": self._used,
                "capacity_bytes": self.max_bytes,
                "attempts": self._attempts,
                "committed_frames": self._committed,
                "sealed": self._sealed,
                "closed": self._closed,
                "fsync_succeeded": self._fsynced,
                "sha256": self._sha256,
                "errors": list(self._errors),
                "error_count": self._error_count,
                "emergency": emergency,
                "incomplete": bool(self._errors)
                or not self._sealed
                or self._retention_incomplete,
                "durability": "process crash with surviving OS/page cache; "
                "seal explicitly flushes/fsyncs; no power-loss guarantee",
            }

    def iter_messages(self) -> Iterator[dict]:
        """Stream recovery in original order, including rejected/future events."""
        yield from self.recover(self.path, self.wave_id).iter_messages()

    @staticmethod
    def recover(path: Path | str, expected_wave_id: str) -> Recovery:
        """Read without mutation after child exit/seal; never infer fsync from bytes."""
        return Recovery(Path(path), expected_wave_id)


class Recovery:
    """Read-only committed-prefix/emergency recovery; no whole-wave message list."""

    def __init__(self, path: Path, expected_wave_id: str) -> None:
        if not _safe_name(expected_wave_id):
            raise ValueError("invalid expected wave identity")
        self.path, self.wave_id = path, expected_wave_id
        self.emergency_path = path.with_name(
            f"{expected_wave_id}.events.emergency.ndjson"
        )
        self._details: dict = {}
        for _message in self._scan():
            pass

    def _normal(self, details: dict) -> Iterator[tuple[int, dict, str]]:
        with self.path.open("rb") as handle:
            header = handle.read(_HEADER_SIZE)
            if len(header) != _HEADER_SIZE:
                raise SpoolError("incomplete spool header")
            prefix = header[: _HEADER.size]
            magic, version, length, capacity, wave_bytes = _HEADER.unpack(prefix)
            if (
                magic != _MAGIC
                or version != 1
                or not 1 <= length <= 64
                or header[_HEADER.size : _HEADER.size + 32]
                != hashlib.sha256(prefix).digest()
                or header[-len(_HEADER_COMMIT) :] != _HEADER_COMMIT
                or not 512 <= capacity <= MAX_BYTES
                or wave_bytes[length:] != b"\0" * (64 - length)
            ):
                raise SpoolError("invalid spool header")
            if wave_bytes[:length] != self.wave_id.encode("ascii"):
                raise SpoolError("spool wave mismatch")
            size = self.path.stat().st_size
            details.update(capacity_bytes=capacity, bytes=size, committed_frames=0)
            if size > capacity:
                details["errors"].append("file_exceeds_capacity")
            chain = hashlib.sha256(header)
            sequence = 0
            while True:
                offset = handle.tell()
                peek = handle.read(8)
                handle.seek(offset)
                if not peek or peek == b"\0" * len(peek):
                    details["tail_offset"] = offset
                    details["errors"].append("unsealed")
                    return
                if peek == _SEAL_MAGIC:
                    data = handle.read(_SEAL.size)
                    if len(data) == _SEAL.size:
                        _magic, count, end, checksum, commit = _SEAL.unpack(data)
                        if (
                            count == sequence
                            and end == offset
                            and checksum == chain.digest()
                            and commit == _COMMIT
                        ):
                            details["seal_marker_present"] = True
                            details["used_bytes"] = handle.tell()
                            if handle.tell() != size:
                                details["errors"].append("seal_not_truncated")
                            return
                    details["errors"].append("invalid_seal")
                    details["tail_offset"] = offset
                    return
                frame = handle.read(_FRAME.size)
                if len(frame) != _FRAME.size:
                    details["errors"].append("partial_frame_header")
                    details["tail_offset"] = offset
                    return
                magic, length, original_sequence, checksum = _FRAME.unpack(frame)
                if (
                    magic != _FRAME_MAGIC
                    or not 0
                    < length
                    <= capacity - _HEADER_SIZE - _FRAME.size - len(_COMMIT)
                ):
                    details["errors"].append("partial_or_invalid_frame")
                    details["tail_offset"] = offset
                    return
                payload, commit = handle.read(length), handle.read(len(_COMMIT))
                digest = hashlib.sha256(struct.pack(">Q", original_sequence) + payload)
                if (
                    len(payload) != length
                    or commit != _COMMIT
                    or digest.digest() != checksum
                    or original_sequence != sequence
                ):
                    details["errors"].append("partial_or_invalid_frame")
                    details["tail_offset"] = offset
                    return
                try:
                    message = _strict_load(payload)
                    if (
                        type(message) is not dict
                        or set(message) != {"kind", "event", "wave_id"}
                        or message["wave_id"] != self.wave_id
                    ):
                        raise ValueError("invalid envelope")
                    if (
                        type(message["kind"]) is not str
                        or _SAFE_KIND.fullmatch(message["kind"]) is None
                        or type(message["event"]) is not dict
                    ):
                        raise ValueError("invalid event")
                    _validate(message["event"])
                except (ValueError, TypeError, UnicodeError):
                    details["errors"].append("invalid_frame_json")
                    details["tail_offset"] = offset
                    return
                chain.update(frame + payload + commit)
                details["committed_frames"] += 1
                details["used_bytes"] = handle.tell()
                sequence += 1
                yield original_sequence, message, hashlib.sha256(payload).hexdigest()

    def _scan(self) -> Iterator[dict]:
        details = {
            "format": "g15-wave-event-spool-recovery-v1",
            "wave_id": self.wave_id,
            "path": self.path.name,
            "errors": [],
            "seal_marker_present": False,
            "fsync_independently_verified": False,
            "emergency_messages": 0,
            "recovered_messages": 0,
            "uncertain_commit_duplicates": 0,
        }
        last_sequence = -1
        last_hash = None
        for sequence, message, digest in self._normal(details):
            last_sequence, last_hash = sequence, digest
            details["recovered_messages"] += 1
            yield message
        if self.emergency_path.exists():
            details["errors"].append("emergency_evidence_present")
            with self.emergency_path.open("rb") as handle:
                for line in handle:
                    try:
                        if not line.endswith(b"\n"):
                            raise ValueError("partial emergency line")
                        row = _strict_load(line)
                        if type(row) is not dict or set(row) != {
                            "format",
                            "wave_id",
                            "sequence",
                            "reason",
                            "serialization_complete",
                            "message",
                            "message_sha256",
                        }:
                            raise ValueError("invalid emergency fields")
                        if (
                            row["format"] != "g15-wave-event-spool-emergency-v1"
                            or type(row["reason"]) is not str
                            or type(row["serialization_complete"]) is not bool
                        ):
                            raise ValueError("invalid emergency types")
                        message = row["message"]
                        sequence = row["sequence"]
                        if (
                            type(message) is not dict
                            or type(message.get("kind")) is not str
                            or _SAFE_KIND.fullmatch(message["kind"]) is None
                        ):
                            raise ValueError("invalid emergency envelope")
                        expected_keys = {"kind", "event", "wave_id"}
                        if not row["serialization_complete"]:
                            expected_keys.add("spool_rejection")
                        if set(message) != expected_keys:
                            raise ValueError("invalid emergency message fields")
                        if row["serialization_complete"]:
                            if type(message["event"]) is not dict:
                                raise ValueError("invalid emergency event")
                            _validate(message["event"])
                        elif type(message["spool_rejection"]) is not str:
                            raise ValueError("invalid rejection projection")
                        checksum = hashlib.sha256()
                        for block in _chunks(message):
                            checksum.update(block)
                        digest = checksum.hexdigest()
                        if (
                            row["wave_id"] != self.wave_id
                            or message["wave_id"] != self.wave_id
                            or row["message_sha256"] != digest
                            or type(sequence) is not int
                            or sequence < 0
                        ):
                            raise ValueError("invalid emergency identity/hash")
                        if not row["serialization_complete"]:
                            details["errors"].append("rejected_input_projection")
                        if sequence == last_sequence and digest == last_hash:
                            details["uncertain_commit_duplicates"] += 1
                            continue
                        if sequence <= last_sequence:
                            raise ValueError("conflicting emergency sequence")
                        if sequence != last_sequence + 1:
                            details["errors"].append("emergency_sequence_gap")
                    except (ValueError, TypeError, KeyError, UnicodeError):
                        details["errors"].append("invalid_or_partial_emergency")
                        continue
                    last_sequence, last_hash = sequence, digest
                    details["emergency_messages"] += 1
                    details["recovered_messages"] += 1
                    yield message
        details["sha256"] = _sha_file(self.path)
        details["emergency"] = (
            {
                "path": self.emergency_path.name,
                "bytes": self.emergency_path.stat().st_size,
                "sha256": _sha_file(self.emergency_path),
            }
            if self.emergency_path.exists()
            else None
        )
        details["incomplete"] = (
            bool(details["errors"]) or not details["seal_marker_present"]
        )
        self._details = details

    def iter_messages(self) -> Iterator[dict]:
        """Validate anew and stream the immutable saved evidence in original order."""
        yield from self._scan()

    def facts(self) -> dict:
        """Recovery cannot attest an earlier flush/fsync or a power failure."""
        return json.loads(json.dumps(self._details))
