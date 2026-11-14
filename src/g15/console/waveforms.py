"""Bounded, read-only raw-sample views for the local console.

The caller authorizes a (run_id, device_id) pair before calling this module.
Offline runs use their complete public manifest; live runs expose only committed
samples whose chunk digests are already persisted in ``processed_chunks``.
Neither raw temperature nor the display-only spectrum is a health-score input.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import zipfile
import zlib

import numpy as np

from g15.live.schema import load_live_session
from g15.pipeline.schema import (
    FIELD_DTYPES,
    FIELD_UNITS,
    NPZ_FIELDS,
    DataContractError,
    ManifestStatus,
    manifest_from_dict,
)

MAX_COUNT = 4096
MAX_FFT_POINTS = 2046  # rfft returns at most 1024 bins, including DC/Nyquist.
MAX_PARTS = 32
MAX_MANIFEST_BYTES = 1_048_576
MAX_MANIFEST_PARTS = 4096
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_DECODED_BYTES = 64 * 1024 * 1024
MAX_CHUNK_POINTS = 1_048_576
MAX_HEADER_BYTES = 4096
MAX_DATABASE_BYTES = 512 * 1024 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class WaveformRequestError(Exception):
    """A stable HTTP status/code pair; never exposes local file paths."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class _Part:
    path: str
    start: int
    rows: int
    digest: str


@dataclass(frozen=True)
class _Source:
    rate: int
    available: int
    kind: str
    integrity_source: str
    parts: tuple[_Part, ...]
    batch_points: int = 0


def _fail(code="waveform_invalid_data", status=409):
    raise WaveformRequestError(status, code)


def _identifier(value):
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or value.endswith(".")
    ):
        _fail("invalid_waveform_request", 400)
    return value


def _integer(value, low, high):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        _fail("invalid_waveform_request", 400)
    return value


def _guard_path(root: Path, path: Path, *, directory=False):
    """Reject links/reparse points in every ancestor and the final component."""
    if not path.is_relative_to(root):
        _fail("waveform_unsafe_path")
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail("waveform_unsafe_path")
        expected = stat.S_ISDIR if item != path or directory else stat.S_ISREG
        if not expected(info.st_mode):
            _fail("waveform_unsafe_path")
    if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        _fail("waveform_unsafe_path")
    return info


def _identity(info):
    return info.st_dev, info.st_ino


def _read_file(root, path, limit):
    before = _guard_path(root, path)
    if before.st_size > limit:
        _fail("waveform_budget_exceeded", 413)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
            _fail("waveform_changed", 503)
        payload = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    final = _guard_path(root, path)
    if (
        _identity(opened) != _identity(final)
        or opened.st_size != after.st_size
        or opened.st_mtime_ns != after.st_mtime_ns
    ):
        _fail("waveform_changed", 503)
    if len(payload) > limit:
        _fail("waveform_budget_exceeded", 413)
    return payload


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _part_path(directory, relative):
    # The public POSIX contract also runs on Windows: refuse ADS/device aliases.
    for component in relative.split("/"):
        stem = component.split(".")[0].upper()
        if (
            ":" in component
            or component.casefold() == "private_truth"
            or component.rstrip(". ") != component
            or stem in {"CON", "PRN", "AUX", "NUL"}
            or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)
        ):
            _fail("waveform_unsafe_path")
    return directory / relative


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _source(root, directory, run_id, device_id):
    has_manifest = _present(directory / "manifest.json")
    has_live = _present(directory / "live-session.json")
    if has_manifest and has_live:
        _fail("waveform_ambiguous_source")
    if has_live:
        _guard_path(root, directory / "live-session.json")
        descriptor = load_live_session(directory, run_id, device_id)
        _guard_path(root, directory, directory=True)
        available = descriptor["processed_samples"]
        if available > MAX_SAFE_INTEGER:
            _fail("waveform_budget_exceeded", 413)
        batch = round(descriptor["sample_rate_hz"] * descriptor["interval_seconds"])
        if available % batch:
            _fail()
        return _Source(
            descriptor["sample_rate_hz"],
            available,
            descriptor["source_kind"],
            "processed_chunks",
            (),
            batch,
        )
    if not has_manifest:
        _fail("waveform_not_found", 404)
    payload = json.loads(
        _read_file(root, directory / "manifest.json", MAX_MANIFEST_BYTES),
        object_pairs_hook=_object,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("parts"), list):
        _fail()
    if len(payload["parts"]) > MAX_MANIFEST_PARTS:
        _fail("waveform_budget_exceeded", 413)
    manifest = manifest_from_dict(payload)
    if manifest.run_id != run_id or manifest.device_id != device_id:
        _fail("waveform_identity_mismatch")
    if manifest.status != ManifestStatus.COMPLETE:
        _fail("waveform_not_ready")
    if manifest.total_points > MAX_SAFE_INTEGER or manifest.sample_rate_hz > 10**9:
        _fail("waveform_budget_exceeded", 413)
    offset = 0
    parts = []
    for part in manifest.parts:
        parts.append(_Part(part.path, offset, part.rows, part.sha256))
        offset += part.rows
    return _Source(
        manifest.sample_rate_hz,
        manifest.total_points,
        "offline_simulator",
        "manifest",
        tuple(parts),
    )


def _live_parts(root, directory, run_id, source, start, end):
    first = start // source.batch_points
    last = (end - 1) // source.batch_points
    if last - first + 1 > MAX_PARTS:
        _fail("waveform_budget_exceeded", 413)
    database = directory / "artifacts" / "results.sqlite"
    before = _guard_path(root, database)
    if before.st_size > MAX_DATABASE_BYTES:
        _fail("waveform_budget_exceeded", 413)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = database.with_name(database.name + suffix)
        if _present(sidecar):
            _guard_path(root, sidecar)
    # mode=ro permits concurrent WAL readers; immutable=1 would miss committed WAL.
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.25)
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        steps = 0

        def bounded_query():
            nonlocal steps
            steps += 1
            return int(steps > 1000)  # at most 100,000 SQLite VM instructions.

        conn.set_progress_handler(bounded_query, 100)
        try:
            rows = conn.execute(
                "SELECT part_id, sha256 FROM processed_chunks "
                "WHERE run_id=? AND part_id BETWEEN ? AND ? "
                "ORDER BY part_id LIMIT ?",
                (run_id, first, last, MAX_PARTS + 1),
            ).fetchall()
        finally:
            conn.set_progress_handler(None, 0)
    if _identity(before) != _identity(_guard_path(root, database)):
        _fail("waveform_changed", 503)
    if len(rows) != last - first + 1:
        _fail("waveform_not_ready", 503)
    parts = []
    for expected, row in zip(range(first, last + 1), rows):
        part_id, digest = row
        if (
            part_id != expected
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            _fail()
        parts.append(
            _Part(
                f"raw/part-{part_id:08d}.npz",
                part_id * source.batch_points,
                source.batch_points,
                digest,
            )
        )
    return parts


def _array(member, info, name, rows):
    """Check a bounded NPY header before allocating or decompressing its array."""
    magic = member.read(8)
    if magic[:6] != b"\x93NUMPY" or magic[6:] not in (b"\x01\x00", b"\x02\x00"):
        _fail()
    version_one = magic[6:] == b"\x01\x00"
    length_size = 2 if version_one else 4
    length_bytes = member.read(length_size)
    if len(length_bytes) != length_size:
        _fail()
    length = struct.unpack("<H" if version_one else "<I", length_bytes)[0]
    if length > MAX_HEADER_BYTES:
        _fail("waveform_budget_exceeded", 413)
    header = member.read(length)
    if len(header) != length or info.file_size != 8 + length_size + length + rows * 8:
        _fail()
    header_reader = (
        np.lib.format.read_array_header_1_0
        if version_one
        else np.lib.format.read_array_header_2_0
    )
    shape, fortran, dtype = header_reader(
        io.BytesIO(length_bytes + header), max_header_size=MAX_HEADER_BYTES
    )
    if shape != (rows,) or fortran or dtype != FIELD_DTYPES[name]:
        _fail()
    data = member.read(rows * 8 + 1)
    if len(data) != rows * 8:
        _fail()
    return np.frombuffer(data, dtype=dtype)


def _chunk(payload, part):
    if hashlib.sha256(payload).hexdigest() != part.digest:
        _fail("waveform_integrity_failed")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        if len(entries) != 4 or {entry.filename for entry in entries} != {
            name + ".npy" for name in NPZ_FIELDS
        }:
            _fail()
        arrays = {}
        for info in entries:
            if (
                info.flag_bits & 1
                or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or not part.rows * 8
                < info.file_size
                <= part.rows * 8 + MAX_HEADER_BYTES + 12
            ):
                _fail()
            name = info.filename[:-4]
            with archive.open(info) as member:
                arrays[name] = _array(member, info, name, part.rows)
    seq = arrays["seq"]
    timestamps = arrays["timestamp_ns"]
    if (
        int(seq[0]) != part.start
        or int(seq[-1]) != part.start + part.rows - 1
        or not np.all(np.diff(seq) == 1)
        or not np.all(timestamps[1:] > timestamps[:-1])
        or not np.isfinite(arrays["vibration"]).all()
        or not np.isfinite(arrays["temperature"]).all()
    ):
        _fail()
    return arrays


def _spectrum(vibration, rate, end):
    points = min(len(vibration), MAX_FFT_POINTS)
    if points < 4:
        return None
    values = vibration[-points:]
    # Scaling first prevents overflow for finite but extreme raw inputs.
    scale = float(np.max(np.abs(values)))
    normalized = values / scale if scale else values
    centered = normalized - np.mean(normalized)
    window = np.hanning(points)
    amplitude = np.abs(np.fft.rfft(centered * window)) / window.sum()
    amplitude[1 : -1 if points % 2 == 0 else None] *= 2
    with np.errstate(over="ignore", invalid="ignore"):
        amplitude *= scale
    if not np.isfinite(amplitude).all():
        _fail("waveform_spectrum_overflow")
    return {
        "frequency_hz": np.fft.rfftfreq(points, d=1.0 / rate).tolist(),
        "amplitude_g": amplitude.tolist(),
        "sample_count": points,
        "fft_points": points,
        "window": "hann",
        "detrended": True,
        "scaling": "single_sided_peak_amplitude",
        "units": "g",
        "start_seq": end - points,
        "end_seq_exclusive": end,
    }


def _read(root, run_id, device_id, start_seq, count):
    directory = root / run_id
    _guard_path(root, directory, directory=True)
    source = _source(root, directory, run_id, device_id)
    if start_seq is not None and (
        (source.available > 0 and start_seq >= source.available)
        or (source.available == 0 and start_seq != 0)
    ):
        _fail("waveform_range_unavailable", 416)
    start = max(0, source.available - count) if start_seq is None else start_seq
    end = min(start + count, source.available)
    parts = []
    if end > start:
        parts = (
            _live_parts(root, directory, run_id, source, start, end)
            if source.batch_points
            else [
                part
                for part in source.parts
                if part.start < end and part.start + part.rows > start
            ]
        )
    if len(parts) > MAX_PARTS or any(part.rows > MAX_CHUNK_POINTS for part in parts):
        _fail("waveform_budget_exceeded", 413)
    if sum(part.rows * 32 + MAX_HEADER_BYTES * 4 for part in parts) > MAX_DECODED_BYTES:
        _fail("waveform_budget_exceeded", 413)
    remaining = MAX_ARCHIVE_BYTES
    selections = {name: [] for name in NPZ_FIELDS}
    for part in parts:
        payload = _read_file(root, _part_path(directory, part.path), remaining)
        remaining -= len(payload)
        arrays = _chunk(payload, part)
        left, right = max(start - part.start, 0), min(end - part.start, part.rows)
        for name in NPZ_FIELDS:
            selections[name].append(arrays[name][left:right].copy())
    arrays = {
        name: (
            np.concatenate(chunks) if chunks else np.array([], dtype=FIELD_DTYPES[name])
        )
        for name, chunks in selections.items()
    }
    if len(arrays["seq"]) != end - start:
        _fail()
    timestamps = [int(value) for value in arrays["timestamp_ns"]]
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        _fail()
    # A spectrum requires the declared regular sampling cadence (1 ns rounding).
    if any(
        abs((right - left) * source.rate - 10**9) > source.rate
        for left, right in zip(timestamps, timestamps[1:])
    ):
        _fail("waveform_invalid_cadence")
    _guard_path(root, directory, directory=True)
    return {
        "run_id": run_id,
        "device_id": device_id,
        "source_kind": source.kind,
        "sample_rate_hz": source.rate,
        "available_points": source.available,
        "start_seq": start,
        "end_seq_exclusive": end,
        "count": end - start,
        "seq": arrays["seq"].tolist(),
        "timestamp_ns": [str(value) for value in timestamps],
        "time_seconds": [(value - timestamps[0]) / 10**9 for value in timestamps],
        "vibration": arrays["vibration"].tolist(),
        "temperature": arrays["temperature"].tolist(),
        "units": dict(FIELD_UNITS),
        "temperature_role": "context_only",
        "integrity": {
            "status": "sha256_verified" if parts else "no_samples",
            "source": source.integrity_source,
        },
        "spectrum": _spectrum(arrays["vibration"], source.rate, end),
    }


def read_waveform(
    runs_root: Path,
    run_id: str,
    device_id: str,
    *,
    start_seq: int | None = None,
    count: int = 2048,
) -> dict:
    """Return actual contiguous raw samples and a bounded vibration spectrum.

    ``count`` is 2..4096. Missing ``start_seq`` selects the latest committed
    samples. An explicit start must be within the available range; a final
    partial page is returned without padding. An empty live run returns empty
    arrays (explicit start zero is also accepted). Nanosecond timestamps are
    decimal strings to preserve int64 precision in browsers. The one-sided Hann
    amplitude spectrum uses at most the final 2046 returned samples, removes the
    mean, corrects Hann coherent gain, and reports peak amplitude in g, not PSD.

    Authorization and concurrent-request limits belong to the console handler;
    this function does not discover runs or read private fault truth.
    """
    _identifier(run_id)
    _identifier(device_id)
    _integer(count, 2, MAX_COUNT)
    if start_seq is not None:
        _integer(start_seq, 0, MAX_SAFE_INTEGER)
    root = Path(os.path.abspath(runs_root))
    try:
        return _read(root, run_id, device_id, start_seq, count)
    except WaveformRequestError:
        raise
    except FileNotFoundError as exc:
        raise WaveformRequestError(404, "waveform_not_found") from exc
    except (OSError, sqlite3.Error) as exc:
        raise WaveformRequestError(503, "waveform_unavailable") from exc
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        EOFError,
        RecursionError,
        zipfile.BadZipFile,
        zlib.error,
        DataContractError,
    ) as exc:
        raise WaveformRequestError(409, "waveform_invalid_data") from exc
