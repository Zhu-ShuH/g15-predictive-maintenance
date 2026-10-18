"""Validated, immutable configuration objects for the G15 sandbox.

The module implements the configuration contract of ``docs/interface.md``
section 4: one YAML document per producer scenario (``scenario`` / ``signals`` /
``fault_generation``) plus a separate consumer document that never carries fault
information. Both are turned into frozen dataclasses, so downstream modules
cannot mutate validated parameters, and every rejection names the offending
field path (for example ``scenario.sample_rate_hz``).

Validation is split into two phases:

* structural parsing (:func:`_build_scenario_config` and its helpers) rejects
  wrong containers, missing keys, unknown keys and unparseable timestamps;
* :func:`validate_config` is the single source of truth for types and values:
  ``type(value) is int`` (so ``True`` never passes as ``1``), ``math.isfinite``,
  positivity, the probability range, the chunk/window divisibility rule and the
  half-open event intervals.

:func:`load_config` always runs both phases, so callers only ever see objects
that passed every check. Loading performs no simulation, writes no files and
does not parse ``health_rules.yaml`` (its content belongs to B2).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path

import yaml

__all__ = [
    "FAULT_MODES",
    "FAULT_TYPES",
    "INCOMPLETE_WINDOW_POLICIES",
    "SCHEMA_VERSION",
    "ConfigError",
    "ConsumerConfig",
    "FaultEvent",
    "FaultGenerationConfig",
    "ScenarioBlock",
    "ScenarioConfig",
    "SignalConfig",
    "SignalsBlock",
    "load_config",
    "load_consumer_config",
    "validate_config",
]

#: Only schema version accepted by this implementation (``docs/interface.md``).
SCHEMA_VERSION = "1.0"

#: Fault mechanisms of ``docs/interface.md`` section 7.2.
FAULT_TYPES = frozenset({"spike", "step", "burst"})

#: Fault generation modes of ``docs/interface.md`` section 4.
FAULT_MODES = frozenset({"manual", "stochastic", "combined"})

#: Policies for a final window shorter than ``window_points``.
INCOMPLETE_WINDOW_POLICIES = frozenset({"drop", "error"})

_SCENARIO_FIELDS = (
    "id",
    "seed",
    "start_time_utc",
    "device_id",
    "sample_rate_hz",
    "total_points",
    "chunk_points",
)
_SCENARIO_OPTIONAL = ("description",)
_SIGNAL_FIELDS = ("baseline", "degradation_rate_per_sample", "noise_std", "unit")
_FAULT_FIELDS = ("mode", "stochastic_probability_per_window", "events")
_EVENT_FIELDS = ("event_id", "type", "start_seq", "duration_points", "amplitude")
_CONSUMER_FIELDS = (
    "schema_version",
    "window_points",
    "incomplete_window",
    "health_rules_file",
)


class ConfigError(ValueError):
    """Raised when a configuration document is rejected.

    The exception is a :class:`ValueError` so that callers guarding against
    numeric problems keep working, and it carries the field path separately so
    that the CLI can report it without string surgery.

    Attributes:
        path: Dotted field path of the offending value, e.g. ``scenario.seed``.
            File-level problems use the file name instead.
        reason: Human readable explanation, without the path prefix.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}" if path else reason)


@dataclass(frozen=True)
class SignalConfig:
    """Parameters of one simulated channel (``signals.<name>``).

    Attributes:
        name: Channel name as written in the document (``vibration`` or
            ``temperature``); derived from the key, never from a field.
        baseline: Value of the channel at ``seq == 0``, in ``unit``.
        degradation_rate_per_sample: Linear trend added per sample, in ``unit``
            per sample. Must be finite and non-negative.
        noise_std: Standard deviation of the additive Gaussian noise, in
            ``unit``. Must be finite and non-negative.
        unit: Physical unit of the channel (``g`` or ``degC``).
    """

    name: str
    baseline: float
    degradation_rate_per_sample: float
    noise_std: float
    unit: str


@dataclass(frozen=True)
class SignalsBlock:
    """The ``signals`` block: exactly the two channels of the frozen contract."""

    vibration: SignalConfig
    temperature: SignalConfig


@dataclass(frozen=True)
class ScenarioBlock:  # pylint: disable=too-many-instance-attributes
    """The ``scenario`` block of ``docs/interface.md`` section 4.

    Attributes:
        scenario_id: Value of the YAML key ``id``; exposed as ``scenario_id``
            so that the attribute does not shadow the builtin.
        description: Free-form human description; may be empty.
        seed: Seed handed to ``numpy.random.default_rng``; must be non-negative.
        start_time_utc: Run start instant, always timezone-aware and UTC.
        device_id: Device identifier carried into the manifest.
        sample_rate_hz: Sampling frequency in hertz; must be positive.
        total_points: Declared run length in samples; must be positive.
        chunk_points: Shard size in samples; must be positive and a multiple of
            the consumer ``window_points``.
    """

    scenario_id: str
    description: str
    seed: int
    start_time_utc: dt.datetime
    device_id: str
    sample_rate_hz: int
    total_points: int
    chunk_points: int


@dataclass(frozen=True)
class FaultEvent:
    """One manually scheduled fault event.

    The covered interval is half-open: ``[start_seq, start_seq +
    duration_points)``.

    Attributes:
        event_id: Identifier unique within the document.
        type: One of :data:`FAULT_TYPES`.
        start_seq: First affected sample; must be non-negative.
        duration_points: Number of affected samples; must be positive.
        amplitude: Strength in the unit of the affected channel; any finite
            value, including zero and negative values.
    """

    event_id: str
    type: str
    start_seq: int
    duration_points: int
    amplitude: float


@dataclass(frozen=True)
class FaultGenerationConfig:
    """The ``fault_generation`` block, consumed by the producer only.

    Attributes:
        mode: One of :data:`FAULT_MODES`.
        stochastic_probability_per_window: Probability of a random fault per
            window; must be within ``[0, 1]``.
        events: Manually scheduled events as an immutable tuple.
    """

    mode: str
    stochastic_probability_per_window: float
    events: tuple[FaultEvent, ...]


@dataclass(frozen=True)
class ScenarioConfig:
    """A fully validated producer configuration.

    The attribute layout mirrors ``docs/interface.md`` section 4 so that
    ``simulation/engine.py`` can keep reading ``config.scenario.*`` and
    ``config.signals.<channel>.*`` structurally.

    Attributes:
        schema_version: Always ``'1.0'`` for objects returned by this module.
        scenario: Run-level parameters.
        signals: The two simulated channels.
        fault_generation: Fault schedule; never handed to the consumer.
    """

    schema_version: str
    scenario: ScenarioBlock
    signals: SignalsBlock
    fault_generation: FaultGenerationConfig


@dataclass(frozen=True)
class ConsumerConfig:
    """A fully validated consumer configuration (``configs/consumer.yaml``).

    Attributes:
        schema_version: Always ``'1.0'``.
        window_points: Aggregation window in samples; must be positive.
        incomplete_window: One of :data:`INCOMPLETE_WINDOW_POLICIES`.
        health_rules_file: The value as written in the document.
        health_rules_path: Absolute path resolved from the repository root, so
            that the result does not depend on the current working directory.
    """

    schema_version: str
    window_points: int
    incomplete_window: str
    health_rules_file: str
    health_rules_path: Path


def _join(prefix: str, name: str) -> str:
    """Return the dotted path ``prefix.name``, tolerating an empty prefix."""
    return f"{prefix}.{name}" if prefix else name


def _as_mapping(value: object, path: str) -> dict[str, object]:
    """Return ``value`` as a mapping, or raise.

    Args:
        value: Parsed YAML node.
        path: Field path used in the error message.

    Returns:
        The mapping.

    Raises:
        ConfigError: if ``value`` is not a mapping.
    """
    if not isinstance(value, dict):
        raise ConfigError(path, f"must be a mapping, got {type(value).__name__}")
    return value


def _as_list(value: object, path: str) -> list[object]:
    """Return ``value`` as a list, or raise.

    Args:
        value: Parsed YAML node.
        path: Field path used in the error message.

    Returns:
        The list.

    Raises:
        ConfigError: if ``value`` is not a list.
    """
    if not isinstance(value, list):
        raise ConfigError(path, f"must be a list, got {type(value).__name__}")
    return value


def _as_int(value: object, path: str) -> int:
    """Return ``value`` as an ``int`` without coercion.

    ``bool`` is rejected even though it is an ``int`` subclass, and numeric
    strings or floats are never converted implicitly.

    Args:
        value: Parsed YAML node.
        path: Field path used in the error message.

    Returns:
        The integer.

    Raises:
        ConfigError: if ``value`` is not an ``int``.
    """
    if isinstance(value, bool):
        raise ConfigError(path, f"must be an integer, got bool ({value!r})")
    if not isinstance(value, int):
        raise ConfigError(
            path, f"must be an integer, got {type(value).__name__} ({value!r})"
        )
    return value


def _as_float(value: object, path: str) -> float:
    """Return ``value`` as a ``float`` without accepting ``bool``.

    Integers are accepted where a float is expected; strings are not.

    Args:
        value: Parsed YAML node.
        path: Field path used in the error message.

    Returns:
        The float value.

    Raises:
        ConfigError: if ``value`` is a ``bool`` or not numeric.
    """
    if isinstance(value, bool):
        raise ConfigError(path, f"must be a number, got bool ({value!r})")
    if not isinstance(value, (int, float)):
        raise ConfigError(
            path, f"must be a number, got {type(value).__name__} ({value!r})"
        )
    return float(value)


def _as_str(value: object, path: str, allow_empty: bool = False) -> str:
    """Return ``value`` as a non-empty string.

    Args:
        value: Parsed YAML node.
        path: Field path used in the error message.
        allow_empty: Whether an empty or blank string is acceptable.

    Returns:
        The string.

    Raises:
        ConfigError: if ``value`` is not a string, or is blank when not allowed.
    """
    if not isinstance(value, str):
        raise ConfigError(
            path, f"must be a non-empty string, got {type(value).__name__} ({value!r})"
        )
    if not allow_empty and not value.strip():
        raise ConfigError(path, "must be a non-empty string, got ''")
    return value


def _check_utc_datetime(value: object, path: str) -> dt.datetime:
    """Return ``value`` as a UTC-aware ``datetime``.

    Args:
        value: Parsed YAML node or already built value.
        path: Field path used in the error message.

    Returns:
        The instant, normalised to UTC.

    Raises:
        ConfigError: if the value is not a timezone-aware UTC instant.
    """
    if not isinstance(value, dt.datetime):
        raise ConfigError(
            path, f"must be a datetime, got {type(value).__name__} ({value!r})"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfigError(
            path, "must include a timezone offset (for example a trailing 'Z')"
        )
    offset = value.utcoffset()
    if offset != dt.timedelta(0):
        raise ConfigError(path, f"must be UTC, got offset {offset}")
    return value.astimezone(dt.timezone.utc)


def _as_utc_datetime(value: object, path: str) -> dt.datetime:
    """Parse an ISO-8601 string into a UTC-aware ``datetime``.

    Args:
        value: Parsed YAML node.
        path: Field path used in the error message.

    Returns:
        The instant, normalised to UTC.

    Raises:
        ConfigError: if the text is not an ISO-8601 timestamp, carries no
            timezone, or is not expressed in UTC.
    """
    if not isinstance(value, str):
        raise ConfigError(
            path, f"must be an ISO-8601 string, got {type(value).__name__} ({value!r})"
        )
    text = value.strip()
    if not text:
        raise ConfigError(path, "must be a non-empty ISO-8601 timestamp")
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(
            path, f"is not a valid ISO-8601 timestamp: {value!r}"
        ) from exc
    return _check_utc_datetime(moment, path)


def _check_fields(
    data: dict[str, object],
    path: str,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> None:
    """Reject keys that the contract does not define.

    Unknown keys are reported with their full path so that a typo such as
    ``chunk_size`` is not silently ignored.

    Args:
        data: Mapping to inspect.
        path: Path prefix of the mapping; empty for the document root.
        required: Keys that must be present.
        optional: Keys that may be present.

    Raises:
        ConfigError: if the mapping contains any other key.
    """
    allowed = tuple(sorted(set(required) | set(optional)))
    unknown = sorted(key for key in data if key not in allowed)
    if not unknown:
        return
    reported = ", ".join(_join(path, key) for key in unknown)
    raise ConfigError(
        path,
        f"unknown field(s): {reported}; allowed fields are {list(allowed)}",
    )


def _require(data: dict[str, object], key: str, path: str) -> object:
    """Return ``data[key]``, or raise naming the missing field.

    Args:
        data: Mapping to read from.
        key: Key that must be present.
        path: Path prefix of the mapping; empty for the document root.

    Returns:
        The raw value.

    Raises:
        ConfigError: if ``key`` is absent.
    """
    if key not in data:
        raise ConfigError(_join(path, key), "missing required field")
    return data[key]


def _check_positive(value: object, path: str) -> int:
    """Return ``value`` as a strictly positive integer.

    Args:
        value: Already built field value.
        path: Field path used in the error message.

    Returns:
        The integer.

    Raises:
        ConfigError: if the value is not an integer or is not positive.
    """
    number = _as_int(value, path)
    if number <= 0:
        raise ConfigError(path, f"must be positive, got {number}")
    return number


def _check_non_negative_int(value: object, path: str) -> int:
    """Return ``value`` as a non-negative integer.

    Args:
        value: Already built field value.
        path: Field path used in the error message.

    Returns:
        The integer.

    Raises:
        ConfigError: if the value is not an integer or is negative.
    """
    number = _as_int(value, path)
    if number < 0:
        raise ConfigError(path, f"must be non-negative, got {number}")
    return number


def _check_finite(value: object, path: str) -> float:
    """Return ``value`` as a finite float.

    Args:
        value: Already built field value.
        path: Field path used in the error message.

    Returns:
        The float.

    Raises:
        ConfigError: if the value is not numeric or not finite.
    """
    number = _as_float(value, path)
    if not math.isfinite(number):
        raise ConfigError(path, f"must be finite, got {number!r}")
    return number


def _check_non_negative(value: object, path: str) -> float:
    """Return ``value`` as a finite, non-negative float.

    Args:
        value: Already built field value.
        path: Field path used in the error message.

    Returns:
        The float.

    Raises:
        ConfigError: if the value is not finite or is negative.
    """
    number = _check_finite(value, path)
    if number < 0:
        raise ConfigError(path, f"must be non-negative, got {number!r}")
    return number


def _check_probability(value: object, path: str) -> float:
    """Return ``value`` as a probability inside ``[0, 1]``.

    Args:
        value: Already built field value.
        path: Field path used in the error message.

    Returns:
        The probability.

    Raises:
        ConfigError: if the value is not finite or outside ``[0, 1]``.
    """
    number = _check_finite(value, path)
    if not 0.0 <= number <= 1.0:
        raise ConfigError(path, f"must be within [0, 1], got {number!r}")
    return number


def _check_choice(value: object, path: str, allowed: frozenset[str]) -> str:
    """Return ``value`` as a member of ``allowed``.

    Args:
        value: Already built field value.
        path: Field path used in the error message.
        allowed: Permitted values.

    Returns:
        The string.

    Raises:
        ConfigError: if the value is not one of ``allowed``.
    """
    if not isinstance(value, str) or value not in allowed:
        raise ConfigError(path, f"must be one of {sorted(allowed)}, got {value!r}")
    return value


def _check_schema_version(value: object) -> str:
    """Return ``value`` as the frozen schema version.

    Args:
        value: Already built field value.

    Returns:
        The schema version.

    Raises:
        ConfigError: if the value is not the string ``'1.0'``.
    """
    if not isinstance(value, str) or value != SCHEMA_VERSION:
        raise ConfigError(
            "schema_version",
            f"must be the string {SCHEMA_VERSION!r}, got {value!r}",
        )
    return value


def _validate_signals(signals: SignalsBlock) -> None:
    """Check both channels of a built ``signals`` block.

    Args:
        signals: Block to validate.

    Raises:
        ConfigError: if a channel carries an invalid value.
    """
    for channel in (signals.vibration, signals.temperature):
        path = f"signals.{channel.name}"
        _check_finite(channel.baseline, f"{path}.baseline")
        _check_non_negative(
            channel.degradation_rate_per_sample,
            f"{path}.degradation_rate_per_sample",
        )
        _check_non_negative(channel.noise_std, f"{path}.noise_std")
        _as_str(channel.unit, f"{path}.unit")


def _validate_scenario(scenario: ScenarioBlock) -> None:
    """Check the run-level parameters.

    Args:
        scenario: Block to validate.

    Raises:
        ConfigError: if a parameter is out of range.
    """
    _as_str(scenario.scenario_id, "scenario.id")
    _as_str(scenario.description, "scenario.description", allow_empty=True)
    _check_non_negative_int(scenario.seed, "scenario.seed")
    _check_utc_datetime(scenario.start_time_utc, "scenario.start_time_utc")
    _as_str(scenario.device_id, "scenario.device_id")
    _check_positive(scenario.sample_rate_hz, "scenario.sample_rate_hz")
    _check_positive(scenario.total_points, "scenario.total_points")
    _check_positive(scenario.chunk_points, "scenario.chunk_points")


def _validate_event(
    event: FaultEvent,
    index: int,
    total_points: int,
    seen: dict[str, int],
) -> None:
    """Check one event against the contract and against its siblings.

    Args:
        event: Event to validate.
        index: Position in ``fault_generation.events``.
        total_points: Declared run length, the exclusive upper bound.
        seen: Mapping of already used ``event_id`` to its first index.

    Raises:
        ConfigError: if the event is malformed, duplicated or out of range.
    """
    path = f"fault_generation.events[{index}]"
    _as_str(event.event_id, f"{path}.event_id")
    if event.event_id in seen:
        first = seen[event.event_id]
        raise ConfigError(
            f"{path}.event_id",
            f"duplicate event_id {event.event_id!r} "
            f"(first defined at fault_generation.events[{first}])",
        )
    seen[event.event_id] = index
    _check_choice(event.type, f"{path}.type", FAULT_TYPES)
    _check_non_negative_int(event.start_seq, f"{path}.start_seq")
    _check_positive(event.duration_points, f"{path}.duration_points")
    _check_finite(event.amplitude, f"{path}.amplitude")
    stop = event.start_seq + event.duration_points
    if stop > total_points:
        raise ConfigError(
            path,
            f"interval [{event.start_seq}, {stop}) exceeds "
            f"scenario.total_points={total_points}",
        )


def _validate_fault_generation(
    fault_generation: FaultGenerationConfig, total_points: int
) -> None:
    """Check the fault schedule.

    Args:
        fault_generation: Block to validate.
        total_points: Declared run length, used as the event upper bound.

    Raises:
        ConfigError: if the mode, probability or an event is invalid.
    """
    _check_choice(fault_generation.mode, "fault_generation.mode", FAULT_MODES)
    _check_probability(
        fault_generation.stochastic_probability_per_window,
        "fault_generation.stochastic_probability_per_window",
    )
    seen: dict[str, int] = {}
    for index, event in enumerate(fault_generation.events):
        _validate_event(event, index, total_points, seen)


def _validate_consumer(consumer: ConsumerConfig) -> None:
    """Check a built consumer configuration without touching the filesystem.

    Args:
        consumer: Configuration to validate.

    Raises:
        ConfigError: if a consumer parameter is invalid.
    """
    _check_schema_version(consumer.schema_version)
    _check_positive(consumer.window_points, "window_points")
    _check_choice(
        consumer.incomplete_window,
        "incomplete_window",
        INCOMPLETE_WINDOW_POLICIES,
    )
    _as_str(consumer.health_rules_file, "health_rules_file")


def _check_window_alignment(chunk_points: int, window_points: int) -> None:
    """Enforce ``chunk_points % window_points == 0``.

    ``docs/interface.md`` section 4 requires every non-final shard to split into
    whole windows, otherwise a crash could lose the cached partial window.

    Args:
        chunk_points: Producer shard size in samples.
        window_points: Consumer window size in samples.

    Raises:
        ConfigError: if the two sizes are not divisible.
    """
    if chunk_points % window_points != 0:
        raise ConfigError(
            "scenario.chunk_points",
            f"{chunk_points} is not a multiple of "
            f"consumer.window_points={window_points}",
        )


def validate_config(
    config: ScenarioConfig, *, consumer: ConsumerConfig | None = None
) -> None:
    """Validate a scenario configuration, optionally against a consumer one.

    The function is pure: it never reads a file and never mutates its argument,
    so it is safe to call on objects built by hand.

    Args:
        config: Scenario configuration to validate.
        consumer: Optional consumer configuration. When given, the cross-document
            rule ``chunk_points % window_points == 0`` is enforced as well.

    Raises:
        ConfigError: if any field violates ``docs/interface.md`` section 4. The
            message always starts with the offending field path.
    """
    _check_schema_version(config.schema_version)
    _validate_scenario(config.scenario)
    _validate_signals(config.signals)
    _validate_fault_generation(config.fault_generation, config.scenario.total_points)
    if consumer is not None:
        _validate_consumer(consumer)
        _check_window_alignment(config.scenario.chunk_points, consumer.window_points)


def _read_mapping(path: str | Path) -> dict[str, object]:
    """Read a YAML document that must be a mapping.

    Args:
        path: File to read.

    Returns:
        The top-level mapping.

    Raises:
        FileNotFoundError: if the file does not exist.
        ConfigError: if the file cannot be read or is not a YAML mapping.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")
    if not config_path.is_file():
        raise ConfigError(str(config_path), "is not a regular file")
    try:
        text = config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(str(config_path), "cannot be decoded as UTF-8") from exc
    except OSError as exc:
        raise ConfigError(str(config_path), f"cannot be read: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(str(config_path), f"not valid YAML: {exc}") from exc
    if data is None:
        raise ConfigError(str(config_path), "document is empty; expected a mapping")
    if not isinstance(data, dict):
        raise ConfigError(
            str(config_path),
            f"top level: expected a mapping, got {type(data).__name__}",
        )
    return data


def _build_signal(name: str, data: object) -> SignalConfig:
    """Build one channel from its raw mapping.

    Args:
        name: Channel name taken from the key.
        data: Raw value of ``signals.<name>``.

    Returns:
        The parsed channel.

    Raises:
        ConfigError: if the block is malformed.
    """
    path = f"signals.{name}"
    block = _as_mapping(data, path)
    _check_fields(block, path, _SIGNAL_FIELDS)
    return SignalConfig(
        name=name,
        baseline=_as_float(_require(block, "baseline", path), f"{path}.baseline"),
        degradation_rate_per_sample=_as_float(
            _require(block, "degradation_rate_per_sample", path),
            f"{path}.degradation_rate_per_sample",
        ),
        noise_std=_as_float(_require(block, "noise_std", path), f"{path}.noise_std"),
        unit=_as_str(_require(block, "unit", path), f"{path}.unit"),
    )


def _build_signals(data: object) -> SignalsBlock:
    """Build the ``signals`` block.

    Args:
        data: Raw value of ``signals``.

    Returns:
        The parsed block.

    Raises:
        ConfigError: if a channel is missing or malformed.
    """
    block = _as_mapping(data, "signals")
    _check_fields(block, "signals", ("vibration", "temperature"))
    return SignalsBlock(
        vibration=_build_signal("vibration", _require(block, "vibration", "signals")),
        temperature=_build_signal(
            "temperature", _require(block, "temperature", "signals")
        ),
    )


def _build_scenario_block(data: object) -> ScenarioBlock:
    """Build the ``scenario`` block.

    Args:
        data: Raw value of ``scenario``.

    Returns:
        The parsed block.

    Raises:
        ConfigError: if a field is missing, unknown or malformed.
    """
    block = _as_mapping(data, "scenario")
    _check_fields(block, "scenario", _SCENARIO_FIELDS, _SCENARIO_OPTIONAL)
    return ScenarioBlock(
        scenario_id=_as_str(_require(block, "id", "scenario"), "scenario.id"),
        description=_as_str(
            block.get("description", ""), "scenario.description", allow_empty=True
        ),
        seed=_as_int(_require(block, "seed", "scenario"), "scenario.seed"),
        start_time_utc=_as_utc_datetime(
            _require(block, "start_time_utc", "scenario"),
            "scenario.start_time_utc",
        ),
        device_id=_as_str(
            _require(block, "device_id", "scenario"), "scenario.device_id"
        ),
        sample_rate_hz=_as_int(
            _require(block, "sample_rate_hz", "scenario"),
            "scenario.sample_rate_hz",
        ),
        total_points=_as_int(
            _require(block, "total_points", "scenario"), "scenario.total_points"
        ),
        chunk_points=_as_int(
            _require(block, "chunk_points", "scenario"), "scenario.chunk_points"
        ),
    )


def _build_event(index: int, data: object) -> FaultEvent:
    """Build one event from its raw mapping.

    Args:
        index: Position in ``fault_generation.events``.
        data: Raw list item.

    Returns:
        The parsed event.

    Raises:
        ConfigError: if the item is missing a field or carries an unknown one.
    """
    path = f"fault_generation.events[{index}]"
    block = _as_mapping(data, path)
    _check_fields(block, path, _EVENT_FIELDS)
    return FaultEvent(
        event_id=_as_str(_require(block, "event_id", path), f"{path}.event_id"),
        type=_as_str(_require(block, "type", path), f"{path}.type"),
        start_seq=_as_int(_require(block, "start_seq", path), f"{path}.start_seq"),
        duration_points=_as_int(
            _require(block, "duration_points", path), f"{path}.duration_points"
        ),
        amplitude=_as_float(_require(block, "amplitude", path), f"{path}.amplitude"),
    )


def _build_fault_generation(data: object) -> FaultGenerationConfig:
    """Build the ``fault_generation`` block.

    Args:
        data: Raw value of ``fault_generation``.

    Returns:
        The parsed block with a tuple of events.

    Raises:
        ConfigError: if a field is missing, unknown or malformed.
    """
    block = _as_mapping(data, "fault_generation")
    _check_fields(block, "fault_generation", _FAULT_FIELDS)
    events = _as_list(
        _require(block, "events", "fault_generation"), "fault_generation.events"
    )
    return FaultGenerationConfig(
        mode=_as_str(
            _require(block, "mode", "fault_generation"), "fault_generation.mode"
        ),
        stochastic_probability_per_window=_as_float(
            _require(block, "stochastic_probability_per_window", "fault_generation"),
            "fault_generation.stochastic_probability_per_window",
        ),
        events=tuple(_build_event(index, item) for index, item in enumerate(events)),
    )


def _build_scenario_config(data: dict[str, object]) -> ScenarioConfig:
    """Build a scenario configuration from a raw document.

    Args:
        data: Top-level mapping of a scenario YAML.

    Returns:
        The parsed configuration, not yet fully validated.

    Raises:
        ConfigError: if the document is malformed.
    """
    _check_fields(
        data, "", ("schema_version", "scenario", "signals", "fault_generation")
    )
    return ScenarioConfig(
        schema_version=_check_schema_version(_require(data, "schema_version", "")),
        scenario=_build_scenario_block(_require(data, "scenario", "")),
        signals=_build_signals(_require(data, "signals", "")),
        fault_generation=_build_fault_generation(
            _require(data, "fault_generation", "")
        ),
    )


def load_config(path: str | Path) -> ScenarioConfig:
    """Load and validate a producer scenario configuration.

    Args:
        path: YAML file following ``docs/interface.md`` section 4.

    Returns:
        A deeply immutable, fully validated configuration.

    Raises:
        FileNotFoundError: if the file does not exist.
        ConfigError: if the document is malformed or violates the contract.
    """
    config = _build_scenario_config(_read_mapping(path))
    validate_config(config)
    return config


def _repository_root(config_path: Path) -> Path:
    """Return the repository root that contains ``config_path``.

    Walks up from the configuration file looking for ``pyproject.toml`` so that
    relative paths resolve identically from any working directory.

    Args:
        config_path: Path of the configuration file being loaded.

    Returns:
        The nearest ancestor holding ``pyproject.toml``, or the file's own
        directory when the marker is absent. The result is always absolute, so
        that paths built from it survive a later change of working directory.
    """
    for parent in (config_path.parent, *config_path.parent.parents):
        if (parent / "pyproject.toml").is_file():
            return parent.resolve()
    return config_path.parent.resolve()


def _resolve_health_rules(config_path: Path, value: str) -> Path:
    """Resolve ``health_rules_file`` against the repository root.

    Args:
        config_path: Path of the consumer configuration file.
        value: Raw ``health_rules_file`` value.

    Returns:
        The absolute path of the rules file.

    Raises:
        ConfigError: if the file does not exist.
    """
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate
    else:
        resolved = _repository_root(config_path) / candidate
    resolved = resolved.resolve()
    if not resolved.is_file():
        raise ConfigError("health_rules_file", f"file not found: {resolved}")
    return resolved


def _build_consumer_config(
    data: dict[str, object], config_path: Path
) -> ConsumerConfig:
    """Build a consumer configuration from a raw document.

    Args:
        data: Top-level mapping of a consumer YAML.
        config_path: Path the document was read from, used for path resolution.

    Returns:
        The parsed configuration, not yet fully validated.

    Raises:
        ConfigError: if the document is malformed.
    """
    _check_fields(data, "", _CONSUMER_FIELDS)
    raw_rules = _as_str(_require(data, "health_rules_file", ""), "health_rules_file")
    return ConsumerConfig(
        schema_version=_check_schema_version(_require(data, "schema_version", "")),
        window_points=_as_int(_require(data, "window_points", ""), "window_points"),
        incomplete_window=_as_str(
            _require(data, "incomplete_window", ""), "incomplete_window"
        ),
        health_rules_file=raw_rules,
        health_rules_path=_resolve_health_rules(config_path, raw_rules),
    )


def load_consumer_config(path: str | Path) -> ConsumerConfig:
    """Load and validate a consumer configuration.

    The consumer document never carries fault information: any
    ``fault_generation`` key is rejected as an unknown field.

    Args:
        path: YAML file following ``docs/interface.md`` section 4.

    Returns:
        A deeply immutable, fully validated consumer configuration.

    Raises:
        FileNotFoundError: if the file does not exist.
        ConfigError: if the document is malformed or violates the contract.
    """
    config_path = Path(path)
    config = _build_consumer_config(_read_mapping(config_path), config_path)
    _validate_consumer(config)
    return config
