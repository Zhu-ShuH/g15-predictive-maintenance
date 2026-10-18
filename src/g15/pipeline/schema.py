"""Data contract for the G15 NPZ shards and the run manifest.

This module is the executable form of ``docs/interface.md`` sections 5.1 and
5.2.  It owns *types and validation only*: no file is opened here, so the same
rules can be applied to data produced in memory, read back from disk, or parsed
from a manifest payload.

Design notes
------------
* ``SimulationChunk`` is **not** defined here.  It currently lives in
  :mod:`g15.simulation.engine` and its final home is tracked by Issue #17, so
  every function below is duck-typed: it only requires the four attributes
  ``seq``, ``timestamp_ns``, ``vibration`` and ``temperature``.  Duplicating the
  type here would give the project two definitions of the same contract.
* Validation raises :class:`DataContractError` subclasses rather than using
  ``assert``, so the checks survive ``python -O`` (``members/C4/AGENT.md``).
* Cross-shard ``seq`` continuity is deliberately *not* checked: it belongs to the
  consumer entry point, which is the only place that sees all parts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

import numpy as np

__all__ = [
    "CHUNK_FILE_PATTERN",
    "DataContractError",
    "ChunkValidationError",
    "ManifestError",
    "ManifestStatus",
    "ChunkManifestEntry",
    "Manifest",
    "NPZ_FIELDS",
    "SCHEMA_VERSION",
    "FIELD_DTYPES",
    "FIELD_UNITS",
    "is_safe_relative_path",
    "validate_chunk",
    "manifest_to_dict",
    "manifest_from_dict",
]

#: Frozen schema version; bumping it is an interface change (section 13).
SCHEMA_VERSION: Final[str] = "1.0"

#: The four arrays every shard carries, in the order they are documented.
NPZ_FIELDS: Final[tuple[str, ...]] = (
    "seq",
    "timestamp_ns",
    "vibration",
    "temperature",
)

#: Exact dtype of each array; anything else is a contract violation.
FIELD_DTYPES: Final[dict[str, np.dtype[Any]]] = {
    "seq": np.dtype(np.int64),
    "timestamp_ns": np.dtype(np.int64),
    "vibration": np.dtype(np.float64),
    "temperature": np.dtype(np.float64),
}

#: Physical unit of each signal, recorded in the manifest.
FIELD_UNITS: Final[dict[str, str]] = {
    "vibration": "g",
    "temperature": "degC",
}

#: Canonical shard file name; ``part_id`` is the numeric group.
CHUNK_FILE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^part-(\d{6,})\.npz$")

_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "run_id",
        "scenario_id",
        "device_id",
        "seed",
        "sample_rate_hz",
        "start_time_utc",
        "total_points",
        "units",
        "status",
        "parts",
    }
)

_ENTRY_KEYS: Final[frozenset[str]] = frozenset({"part_id", "path", "rows", "sha256"})


class DataContractError(Exception):
    """Base class for every violation of the shard/manifest contract.

    The CLI maps this family to exit code ``3`` (I/O or schema failure), so all
    rejections raised by :mod:`g15.pipeline.schema` and
    :mod:`g15.pipeline.serialization` inherit from it.
    """


class ChunkValidationError(DataContractError):
    """A shard's arrays do not satisfy ``docs/interface.md`` section 5.1."""


class ManifestError(DataContractError):
    """A manifest payload or entry does not satisfy section 5.2."""


class ManifestStatus(str, Enum):
    """Publication state of a run directory.

    Only :attr:`COMPLETE` is consumable (section 5.2).  :attr:`FAILED` exists so
    a broken run can be inspected without being mistaken for a finished one.
    """

    COMPLETE = "complete"
    FAILED = "failed"


def is_safe_relative_path(value: object) -> bool:
    """Return whether ``value`` may be used as a manifest-relative POSIX path.

    A safe path is a non-empty string, uses ``/`` separators, is not absolute,
    carries no drive letter, and contains no ``.`` or ``..`` component.  The
    function answers the *string* question only; resolving it against a run
    directory is the consumer's job.

    Args:
        value: Candidate path from a manifest entry.

    Returns:
        ``True`` if the path passes every rule above.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    if "\\" in value:
        return False
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


@dataclass(frozen=True)
class ChunkManifestEntry:
    """One ``parts`` element of the manifest (section 5.2).

    Attributes:
        part_id: Zero-based shard index, contiguous across the run.
        path: POSIX path relative to the run directory, e.g.
            ``raw/part-000000.npz``.
        rows: Number of samples stored in the shard.
        sha256: Lower-case hexadecimal SHA-256 of the shard file.
    """

    part_id: int
    path: str
    rows: int
    sha256: str

    def __post_init__(self) -> None:
        """Validate the entry against section 5.2.

        Raises:
            ManifestError: for a negative ``part_id``, an unsafe or non-POSIX
                path, a non-positive ``rows``, or a malformed digest.
        """
        if not isinstance(self.part_id, int) or isinstance(self.part_id, bool):
            raise ManifestError(f"part_id must be an integer, got {self.part_id!r}")
        if self.part_id < 0:
            raise ManifestError(f"part_id must be non-negative, got {self.part_id}")
        if not is_safe_relative_path(self.path):
            raise ManifestError(
                "path must be a relative POSIX path without '.' or '..' components, "
                f"got {self.path!r}"
            )
        if not isinstance(self.rows, int) or isinstance(self.rows, bool):
            raise ManifestError(f"rows must be an integer, got {self.rows!r}")
        if self.rows <= 0:
            raise ManifestError(f"rows must be positive, got {self.rows}")
        if not isinstance(self.sha256, str) or not _SHA256_PATTERN.match(self.sha256):
            raise ManifestError(
                "sha256 must be 64 lower-case hexadecimal characters, got "
                f"{self.sha256!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready ``parts`` element with exactly the frozen keys."""
        return {
            "part_id": self.part_id,
            "path": self.path,
            "rows": self.rows,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class Manifest:
    """Run-level metadata published last, after every shard is durable.

    Attributes:
        schema_version: Must equal :data:`SCHEMA_VERSION`.
        run_id: Identifier of the run directory.
        scenario_id: Scenario identifier copied from the configuration.
        device_id: Device the run belongs to; one run carries one device.
        seed: Simulation seed, needed to reproduce the run.
        sample_rate_hz: Sampling frequency used by the producer.
        start_time_utc: ISO-8601 UTC start instant of the first sample.
        total_points: Declared number of samples in the run.
        units: Physical unit per signal, ``vibration`` and ``temperature``.
        status: Publication state; only ``complete`` may be consumed.
        parts: Shards in ascending ``part_id`` order.
    """

    # A manifest is a flat record; splitting it would only obscure the contract.
    # pylint: disable=too-many-instance-attributes

    schema_version: str
    run_id: str
    scenario_id: str
    device_id: str
    seed: int
    sample_rate_hz: int
    start_time_utc: str
    total_points: int
    units: dict[str, str]
    status: ManifestStatus
    parts: tuple[ChunkManifestEntry, ...]

    def __post_init__(self) -> None:
        """Validate the manifest, including the completeness invariant.

        Raises:
            ManifestError: if the schema version is wrong, an identifier is
                empty, a count is not positive, the units are not exactly the
                two documented signals, the parts are not contiguous from zero,
                or a ``complete`` manifest does not cover ``total_points``.
        """
        if self.schema_version != SCHEMA_VERSION:
            raise ManifestError(
                f"schema_version must be {SCHEMA_VERSION!r}, got "
                f"{self.schema_version!r}"
            )
        for name in ("run_id", "scenario_id", "start_time_utc"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(f"{name} must be a non-empty string, got {value!r}")
        if not isinstance(self.device_id, str):
            raise ManifestError(f"device_id must be a string, got {self.device_id!r}")
        for name in ("seed", "sample_rate_hz", "total_points"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ManifestError(f"{name} must be an integer, got {value!r}")
        if self.sample_rate_hz <= 0:
            raise ManifestError(
                f"sample_rate_hz must be positive, got {self.sample_rate_hz}"
            )
        if self.total_points <= 0:
            raise ManifestError(
                f"total_points must be positive, got {self.total_points}"
            )
        if set(self.units) != set(FIELD_UNITS):
            raise ManifestError(
                f"units must describe exactly {sorted(FIELD_UNITS)}, got "
                f"{sorted(self.units)}"
            )
        if any(not isinstance(value, str) for value in self.units.values()):
            raise ManifestError("every unit must be a string")
        if not isinstance(self.status, ManifestStatus):
            raise ManifestError(f"status must be a ManifestStatus, got {self.status!r}")
        self._check_parts()

    def _check_parts(self) -> None:
        """Check that ``parts`` is contiguous from zero and covers the run.

        Raises:
            ManifestError: on an empty, misnumbered, duplicated or short part list.
        """
        if not self.parts:
            raise ManifestError("parts must not be empty")
        actual = [entry.part_id for entry in self.parts]
        expected = list(range(len(self.parts)))
        if actual != expected:
            raise ManifestError(
                f"part_id must be contiguous from 0, got {actual} for "
                f"{len(self.parts)} parts"
            )
        if self.status is ManifestStatus.COMPLETE:
            total = sum(entry.rows for entry in self.parts)
            if total != self.total_points:
                raise ManifestError(
                    "a complete manifest must cover total_points exactly, got "
                    f"{total} rows for total_points={self.total_points}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-ready manifest with exactly the frozen key set."""
        return manifest_to_dict(self)


def _attribute(source: object, name: str) -> Any:
    """Return ``source.<name>`` or raise a contract error naming the field.

    Args:
        source: Object carrying the chunk arrays.
        name: Attribute to read.

    Returns:
        The attribute value.

    Raises:
        ChunkValidationError: if the attribute is missing.
    """
    try:
        return getattr(source, name)
    except AttributeError as exc:
        raise ChunkValidationError(f"chunk has no field {name!r}") from exc


def _validate_array(name: str, value: object) -> np.ndarray[Any, Any]:
    """Check one shard array's dimensionality and dtype.

    Args:
        name: Field name, used in error messages.
        value: Raw attribute value.

    Returns:
        The value as a NumPy array.

    Raises:
        ChunkValidationError: if it is not a one-dimensional array of the
            frozen dtype.
    """
    array = np.asarray(value)
    expected = FIELD_DTYPES[name]
    if array.ndim != 1:
        raise ChunkValidationError(
            f"{name} must be one-dimensional, got ndim={array.ndim}"
        )
    if array.dtype != expected:
        raise ChunkValidationError(f"{name} must be {expected}, got {array.dtype}")
    return array


def validate_chunk(chunk: object) -> None:
    """Validate one shard against ``docs/interface.md`` section 5.1.

    Checks, in order: the four fields exist, each is one-dimensional with the
    frozen dtype, all four are equally long, the shard is not empty, ``seq``
    increases by exactly one, and both signals are finite.  Cross-shard
    continuity is out of scope here.

    Args:
        chunk: Object exposing ``seq``, ``timestamp_ns``, ``vibration`` and
            ``temperature`` — normally a ``SimulationChunk``.

    Raises:
        ChunkValidationError: on the first violated rule.
    """
    arrays = {
        name: _validate_array(name, _attribute(chunk, name)) for name in NPZ_FIELDS
    }

    lengths = {name: int(array.shape[0]) for name, array in arrays.items()}
    if len(set(lengths.values())) > 1:
        raise ChunkValidationError(f"chunk arrays must be equally long, got {lengths}")
    if lengths["seq"] == 0:
        raise ChunkValidationError("an empty shard must not be written")

    seq = arrays["seq"]
    if not bool(np.all(np.diff(seq) == 1)):
        raise ChunkValidationError(
            "seq must be contiguous and strictly increasing by 1 within a shard"
        )
    for name in ("vibration", "temperature"):
        values = arrays[name]
        finite = np.isfinite(values)
        if not bool(np.all(finite)):
            index = int(np.argmax(~finite))
            raise ChunkValidationError(
                f"{name} is not finite at local index {index} "
                f"(absolute seq {int(seq[index])})"
            )


def manifest_to_dict(manifest: Manifest) -> dict[str, Any]:
    """Serialise ``manifest`` to the JSON-ready mapping of section 5.2.

    Args:
        manifest: A validated manifest.

    Returns:
        A mapping whose key set is exactly the frozen manifest keys.
    """
    return {
        "schema_version": manifest.schema_version,
        "run_id": manifest.run_id,
        "scenario_id": manifest.scenario_id,
        "device_id": manifest.device_id,
        "seed": manifest.seed,
        "sample_rate_hz": manifest.sample_rate_hz,
        "start_time_utc": manifest.start_time_utc,
        "total_points": manifest.total_points,
        "units": dict(manifest.units),
        "status": manifest.status.value,
        "parts": [entry.to_dict() for entry in manifest.parts],
    }


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    """Return ``value`` as a mapping or raise a contract error.

    Args:
        value: Candidate mapping.
        path: Dotted path used in the error message.

    Returns:
        The value as a mapping.

    Raises:
        ManifestError: if the value is not a mapping.
    """
    if not isinstance(value, Mapping):
        raise ManifestError(f"{path} must be an object, got {type(value).__name__}")
    return value


def _require_keys(
    payload: Mapping[str, Any], expected: frozenset[str], path: str
) -> None:
    """Reject a payload whose key set is not exactly ``expected``.

    Unknown keys are refused rather than ignored: a typo in a field name would
    otherwise silently drop data from a consumed run.

    Args:
        payload: Mapping being parsed.
        expected: Exact key set required.
        path: Dotted path used in the error message.

    Raises:
        ManifestError: listing the missing and the unknown keys.
    """
    actual = set(payload)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    raise ManifestError(
        f"{path} has the wrong key set; missing={missing}, unknown={unknown}"
    )


def _require_int(payload: Mapping[str, Any], name: str, path: str) -> int:
    """Return an integer field, rejecting bools and non-integral values.

    Args:
        payload: Mapping being parsed.
        name: Field name.
        path: Dotted path used in the error message.

    Returns:
        The field as a Python int.

    Raises:
        ManifestError: if the value is not integral.
    """
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{path}.{name} must be an integer, got {value!r}")
    return value


def _entry_from_dict(payload: object, path: str) -> ChunkManifestEntry:
    """Build one :class:`ChunkManifestEntry` from a parsed JSON object.

    Args:
        payload: Raw ``parts`` element.
        path: Dotted path used in the error message.

    Returns:
        The validated entry.

    Raises:
        ManifestError: on a non-mapping payload, a wrong key set, or a value
            rejected by :meth:`ChunkManifestEntry.__post_init__`.
    """
    mapping = _require_mapping(payload, path)
    _require_keys(mapping, _ENTRY_KEYS, path)
    sha256 = mapping["sha256"]
    if not isinstance(sha256, str):
        raise ManifestError(f"{path}.sha256 must be a string, got {sha256!r}")
    file_path = mapping["path"]
    if not isinstance(file_path, str):
        raise ManifestError(f"{path}.path must be a string, got {file_path!r}")
    return ChunkManifestEntry(
        part_id=_require_int(mapping, "part_id", path),
        path=file_path,
        rows=_require_int(mapping, "rows", path),
        sha256=sha256,
    )


def manifest_from_dict(payload: object) -> Manifest:
    """Parse and validate a manifest payload.

    The parser is strict on purpose: the schema version must match exactly and
    unknown keys are refused, so a stale or hand-edited manifest cannot be
    consumed by accident.

    Args:
        payload: Decoded JSON object of a manifest.

    Returns:
        The validated :class:`Manifest`.

    Raises:
        ManifestError: on a malformed payload, a wrong key set, an unknown
            ``status``, or any value violating section 5.2.
    """
    mapping = _require_mapping(payload, "manifest")
    _require_keys(mapping, _MANIFEST_KEYS, "manifest")

    version = mapping["schema_version"]
    if version != SCHEMA_VERSION:
        raise ManifestError(
            f"manifest.schema_version must be {SCHEMA_VERSION!r}, got {version!r}"
        )

    status = mapping["status"]
    if not isinstance(status, str):
        raise ManifestError(f"manifest.status must be a string, got {status!r}")
    try:
        parsed_status = ManifestStatus(status)
    except ValueError as exc:
        allowed = [member.value for member in ManifestStatus]
        raise ManifestError(
            f"manifest.status must be one of {allowed}, got {status!r}"
        ) from exc

    units = _require_mapping(mapping["units"], "manifest.units")
    raw_parts = mapping["parts"]
    if isinstance(raw_parts, (str, bytes)) or not isinstance(raw_parts, Sequence):
        raise ManifestError(
            f"manifest.parts must be a sequence, got {type(raw_parts).__name__}"
        )
    parts = tuple(
        _entry_from_dict(item, f"manifest.parts[{index}]")
        for index, item in enumerate(raw_parts)
    )

    for name in ("run_id", "scenario_id", "device_id", "start_time_utc"):
        if not isinstance(mapping[name], str):
            raise ManifestError(
                f"manifest.{name} must be a string, got {mapping[name]!r}"
            )

    return Manifest(
        schema_version=version,
        run_id=mapping["run_id"],
        scenario_id=mapping["scenario_id"],
        device_id=mapping["device_id"],
        seed=_require_int(mapping, "seed", "manifest"),
        sample_rate_hz=_require_int(mapping, "sample_rate_hz", "manifest"),
        start_time_utc=mapping["start_time_utc"],
        total_points=_require_int(mapping, "total_points", "manifest"),
        units={str(key): str(value) for key, value in units.items()},
        status=parsed_status,
        parts=parts,
    )
