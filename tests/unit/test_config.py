"""Unit tests for the configuration contract (Issue #4, owner C3).

The suite covers the six shipped documents, every rejection rule of
``docs/interface.md`` section 4, the deep immutability of the returned objects
and the CLI exit codes of section 10.

Every negative case asserts that the exception message names the offending
field path, because a rejection without a path is useless to whoever has to fix
the YAML.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from g15.cli import main
from g15.config import (
    SCHEMA_VERSION,
    ConfigError,
    ConsumerConfig,
    ScenarioConfig,
    load_config,
    load_consumer_config,
    validate_config,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"
SCENARIO_NAMES = ("default", "healthy", "degradation", "sudden_fault", "benchmark_1m")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def base_document() -> dict:
    """Return a deep copy of the shipped ``default.yaml`` as a plain mapping."""
    text = (CONFIGS / "default.yaml").read_text(encoding="utf-8")
    return copy.deepcopy(yaml.safe_load(text))


def write_document(
    tmp_path: Path, document: object, name: str = "scenario.yaml"
) -> Path:
    """Write ``document`` as YAML and return its path."""
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return path


def consumer_document(tmp_path: Path) -> dict:
    """Return a valid consumer document with a resolvable rules file."""
    (tmp_path / "health_rules.yaml").write_text("rules: []\n", encoding="utf-8")
    return {
        "schema_version": "1.0",
        "window_points": 256,
        "incomplete_window": "drop",
        "health_rules_file": "health_rules.yaml",
    }


def expect_invalid(
    tmp_path: Path,
    document: object,
    *fragments: str,
    name: str = "scenario.yaml",
    loader: object = load_config,
) -> str:
    """Assert that ``document`` is rejected and the message names ``fragments``."""
    path = write_document(tmp_path, document, name=name)
    with pytest.raises(ConfigError) as excinfo:
        loader(path)  # type: ignore[operator]
    message = str(excinfo.value)
    for fragment in fragments:
        assert fragment in message, message
    return message


# --------------------------------------------------------------------------- #
# Legal documents
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_shipped_scenario_configs_validate(name: str) -> None:
    """All five shipped scenario documents must load into the frozen model."""
    config = load_config(CONFIGS / f"{name}.yaml")

    assert isinstance(config, ScenarioConfig)
    assert config.schema_version == SCHEMA_VERSION
    assert validate_config(config) is None


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_shipped_scenarios_align_with_the_consumer_window(name: str) -> None:
    """Every shipped chunk size must split into whole consumer windows."""
    config = load_config(CONFIGS / f"{name}.yaml")
    consumer = load_consumer_config(CONFIGS / "consumer.yaml")

    assert config.scenario.chunk_points % consumer.window_points == 0
    assert validate_config(config, consumer=consumer) is None


def test_repeated_loading_is_deterministic() -> None:
    """Parsing the same document twice must yield equal, independent objects."""
    first = load_config(CONFIGS / "default.yaml")
    second = load_config(CONFIGS / "default.yaml")

    assert first == second
    assert first is not second


def test_shipped_consumer_config_validates() -> None:
    """``consumer.yaml`` must load and resolve its rules file."""
    config = load_consumer_config(CONFIGS / "consumer.yaml")

    assert config.schema_version == SCHEMA_VERSION
    assert config.window_points == 256
    assert config.incomplete_window == "drop"
    assert config.health_rules_path.is_absolute()
    assert config.health_rules_path.is_file()


def test_default_config_matches_the_contract_example() -> None:
    """The default document must reproduce the example of the interface."""
    config = load_config(CONFIGS / "default.yaml")
    scenario = config.scenario

    assert scenario.scenario_id == "bearing_001"
    assert scenario.device_id == "DEV-001"
    assert scenario.seed == 42
    assert scenario.sample_rate_hz == 100
    assert scenario.total_points == 1_000_000
    assert scenario.chunk_points == 102_400

    assert config.signals.vibration.baseline == 0.5
    assert config.signals.vibration.unit == "g"
    assert config.signals.temperature.baseline == 35.0
    assert config.signals.temperature.unit == "degC"

    assert config.fault_generation.mode == "manual"
    assert config.fault_generation.stochastic_probability_per_window == 0.0
    event = config.fault_generation.events[0]
    assert (event.event_id, event.type) == ("F001", "spike")
    assert (event.start_seq, event.duration_points, event.amplitude) == (
        600_000,
        256,
        5.0,
    )


def test_start_time_is_normalised_to_utc() -> None:
    """The start instant must be timezone-aware and expressed in UTC."""
    start = load_config(CONFIGS / "default.yaml").scenario.start_time_utc

    assert start.tzinfo is not None
    assert start.utcoffset() == dt.timedelta(0)
    assert start == dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc)


def test_healthy_scenario_has_no_events() -> None:
    """The healthy baseline must carry an empty, still immutable event tuple."""
    config = load_config(CONFIGS / "healthy.yaml")

    assert config.fault_generation.events == ()
    assert isinstance(config.fault_generation.events, tuple)


def test_consumer_config_carries_no_fault_information() -> None:
    """Neither the consumer model nor its document may mention faults."""
    config = load_consumer_config(CONFIGS / "consumer.yaml")
    names = {field.name for field in dataclasses.fields(config)}

    assert not any("fault" in name for name in names)
    text = (CONFIGS / "consumer.yaml").read_text(encoding="utf-8").lower()
    assert "fault" not in text


# --------------------------------------------------------------------------- #
# Deep immutability
# --------------------------------------------------------------------------- #


def test_scenario_config_is_frozen() -> None:
    """Validated objects must reject attribute assignment at every level."""
    config = load_config(CONFIGS / "default.yaml")

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.schema_version = "2.0"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.scenario.seed = 1  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.signals.vibration.noise_std = 0.0  # type: ignore[misc]


def test_nested_collections_are_immutable() -> None:
    """``events`` must be a tuple and its items frozen dataclasses."""
    config = load_config(CONFIGS / "default.yaml")

    assert isinstance(config.fault_generation.events, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.fault_generation.events[0].event_id = "F999"  # type: ignore[misc]


def test_engine_can_read_the_new_configuration() -> None:
    """B1 reads ``config.scenario.*`` and ``config.signals.*`` structurally."""
    from g15.simulation.engine import load_scenario_parameters

    params = load_scenario_parameters(load_config(CONFIGS / "default.yaml"))

    assert params.seed == 42
    assert params.sample_rate_hz == 100
    assert params.total_points == 1_000_000
    assert params.vibration.unit == "g"
    assert params.temperature.unit == "degC"


# --------------------------------------------------------------------------- #
# File and document level failures
# --------------------------------------------------------------------------- #


def test_missing_file_is_reported(tmp_path: Path) -> None:
    """A missing path must raise ``FileNotFoundError`` naming the file."""
    missing = tmp_path / "nope.yaml"

    with pytest.raises(FileNotFoundError) as excinfo:
        load_config(missing)

    assert "nope.yaml" in str(excinfo.value)


def test_invalid_yaml_syntax_is_reported(tmp_path: Path) -> None:
    """A broken document must be reported as a YAML problem, not a crash."""
    path = tmp_path / "broken.yaml"
    path.write_text("scenario: [unclosed\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "not valid YAML" in str(excinfo.value)


def test_empty_document_is_reported(tmp_path: Path) -> None:
    """An empty file is not a mapping and must say so."""
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "document is empty" in str(excinfo.value)


def test_non_mapping_document_is_reported(tmp_path: Path) -> None:
    """A top-level list must be rejected with the expected type named."""
    path = tmp_path / "list.yaml"
    path.write_text("- schema_version\n- scenario\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "expected a mapping" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Field presence and unknown keys
# --------------------------------------------------------------------------- #


def test_unknown_top_level_field_is_rejected(tmp_path: Path) -> None:
    """The legacy ``manifest`` block must not survive silently."""
    document = base_document()
    document["manifest"] = {"require_complete": True}

    expect_invalid(tmp_path, document, "unknown field", "manifest")


def test_unknown_scenario_field_names_the_full_path(tmp_path: Path) -> None:
    """A typo inside ``scenario`` must be reported with its full path."""
    document = base_document()
    document["scenario"]["chunk_size"] = 1024

    expect_invalid(tmp_path, document, "scenario.chunk_size", "unknown field")


def test_unknown_signal_field_is_rejected(tmp_path: Path) -> None:
    """Unknown channel parameters are rejected, not ignored."""
    document = base_document()
    document["signals"]["vibration"]["gain"] = 1.0

    expect_invalid(tmp_path, document, "signals.vibration.gain")


def test_unknown_event_field_is_rejected(tmp_path: Path) -> None:
    """Unknown event parameters are rejected with an indexed path."""
    document = base_document()
    document["fault_generation"]["events"][0]["frequency"] = 10.0

    expect_invalid(tmp_path, document, "fault_generation.events[0].frequency")


def test_missing_required_field_is_reported(tmp_path: Path) -> None:
    """A missing key must name the exact field."""
    document = base_document()
    del document["scenario"]["sample_rate_hz"]

    expect_invalid(tmp_path, document, "scenario.sample_rate_hz", "missing")


def test_missing_signal_channel_is_reported(tmp_path: Path) -> None:
    """Both channels are mandatory."""
    document = base_document()
    del document["signals"]["temperature"]

    expect_invalid(tmp_path, document, "signals.temperature", "missing")


def test_missing_event_field_is_reported(tmp_path: Path) -> None:
    """An event without an amplitude is invalid, not defaulted."""
    document = base_document()
    del document["fault_generation"]["events"][0]["amplitude"]

    expect_invalid(tmp_path, document, "fault_generation.events[0].amplitude")


def test_events_must_be_a_list(tmp_path: Path) -> None:
    """``events`` must be a sequence, even when empty."""
    document = base_document()
    document["fault_generation"]["events"] = {}

    expect_invalid(tmp_path, document, "fault_generation.events", "must be a list")


def test_schema_version_must_be_exactly_1_0(tmp_path: Path) -> None:
    """A different version is a contract violation, not a warning."""
    document = base_document()
    document["schema_version"] = "2.0"

    expect_invalid(tmp_path, document, "schema_version", "must be the string '1.0'")


def test_schema_version_must_be_a_string(tmp_path: Path) -> None:
    """A float ``1.0`` must not be accepted as the frozen string version."""
    document = base_document()
    document["schema_version"] = 1.0

    expect_invalid(tmp_path, document, "schema_version", "must be the string '1.0'")


# --------------------------------------------------------------------------- #
# Type rules
# --------------------------------------------------------------------------- #


def test_bool_is_not_accepted_as_int(tmp_path: Path) -> None:
    """``True`` is an ``int`` subclass but never a valid seed."""
    document = base_document()
    document["scenario"]["seed"] = True

    expect_invalid(tmp_path, document, "scenario.seed", "got bool")


def test_string_is_not_coerced_to_int(tmp_path: Path) -> None:
    """``"1000000"`` must not be converted implicitly."""
    document = base_document()
    document["scenario"]["total_points"] = "1000000"

    expect_invalid(tmp_path, document, "scenario.total_points", "got str")


def test_float_is_not_accepted_as_int(tmp_path: Path) -> None:
    """A fractional sample rate is not an integer."""
    document = base_document()
    document["scenario"]["sample_rate_hz"] = 100.5

    expect_invalid(tmp_path, document, "scenario.sample_rate_hz", "got float")


def test_nan_is_rejected(tmp_path: Path) -> None:
    """``.nan`` parses as a float and must be refused as non-finite."""
    document = base_document()
    document["signals"]["vibration"]["noise_std"] = float("nan")

    expect_invalid(tmp_path, document, "signals.vibration.noise_std", "must be finite")


def test_infinity_is_rejected(tmp_path: Path) -> None:
    """``.inf`` must be refused as non-finite."""
    document = base_document()
    document["signals"]["temperature"]["baseline"] = float("inf")

    expect_invalid(tmp_path, document, "signals.temperature.baseline", "must be finite")


def test_non_string_unit_is_rejected(tmp_path: Path) -> None:
    """A unit is documentation for humans; it must be a string."""
    document = base_document()
    document["signals"]["vibration"]["unit"] = 7

    expect_invalid(tmp_path, document, "signals.vibration.unit", "non-empty string")


# --------------------------------------------------------------------------- #
# Numeric ranges and cross-field rules
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("field", "value"),
    [("total_points", 0), ("sample_rate_hz", 0), ("chunk_points", -1)],
)
def test_non_positive_integers_are_rejected(
    tmp_path: Path, field: str, value: int
) -> None:
    """Lengths, rates and shard sizes must be strictly positive."""
    document = base_document()
    document["scenario"][field] = value

    expect_invalid(tmp_path, document, f"scenario.{field}", "must be positive")


def test_negative_seed_is_rejected(tmp_path: Path) -> None:
    """``default_rng`` requires a non-negative seed."""
    document = base_document()
    document["scenario"]["seed"] = -1

    expect_invalid(tmp_path, document, "scenario.seed", "must be non-negative")


def test_negative_degradation_rate_is_rejected(tmp_path: Path) -> None:
    """Self-repair is not modelled."""
    document = base_document()
    document["signals"]["temperature"]["degradation_rate_per_sample"] = -1e-6

    expect_invalid(
        tmp_path,
        document,
        "signals.temperature.degradation_rate_per_sample",
        "must be non-negative",
    )


def test_negative_noise_is_rejected(tmp_path: Path) -> None:
    """A standard deviation cannot be negative."""
    document = base_document()
    document["signals"]["vibration"]["noise_std"] = -0.01

    expect_invalid(tmp_path, document, "signals.vibration.noise_std", "non-negative")


@pytest.mark.parametrize("value", [1.5, -0.1])
def test_probability_outside_unit_interval_is_rejected(
    tmp_path: Path, value: float
) -> None:
    """The stochastic probability is a probability."""
    document = base_document()
    document["fault_generation"]["stochastic_probability_per_window"] = value

    expect_invalid(
        tmp_path,
        document,
        "fault_generation.stochastic_probability_per_window",
        "must be within [0, 1]",
    )


def test_unsupported_fault_mode_is_rejected(tmp_path: Path) -> None:
    """Only the three documented modes exist."""
    document = base_document()
    document["fault_generation"]["mode"] = "random"

    expect_invalid(tmp_path, document, "fault_generation.mode", "must be one of")


def test_chunk_points_must_be_a_multiple_of_window_points() -> None:
    """The cross-document divisibility rule must name both fields."""
    config = load_config(CONFIGS / "default.yaml")
    consumer = ConsumerConfig(
        schema_version="1.0",
        window_points=300,
        incomplete_window="drop",
        health_rules_file="configs/health_rules.yaml",
        health_rules_path=CONFIGS / "health_rules.yaml",
    )

    with pytest.raises(ConfigError) as excinfo:
        validate_config(config, consumer=consumer)

    message = str(excinfo.value)
    assert "scenario.chunk_points" in message
    assert "consumer.window_points=300" in message


# --------------------------------------------------------------------------- #
# Time stamps
# --------------------------------------------------------------------------- #


def test_naive_start_time_is_rejected(tmp_path: Path) -> None:
    """A timestamp without a zone is ambiguous and must be refused."""
    document = base_document()
    document["scenario"]["start_time_utc"] = "2026-10-14T00:00:00"

    expect_invalid(tmp_path, document, "scenario.start_time_utc", "timezone")


def test_non_utc_offset_is_rejected(tmp_path: Path) -> None:
    """The contract says UTC, so ``+08:00`` is refused rather than converted."""
    document = base_document()
    document["scenario"]["start_time_utc"] = "2026-10-14T08:00:00+08:00"

    expect_invalid(tmp_path, document, "scenario.start_time_utc", "must be UTC")


def test_unparseable_start_time_is_rejected(tmp_path: Path) -> None:
    """A human string is not an ISO-8601 instant."""
    document = base_document()
    document["scenario"]["start_time_utc"] = "yesterday"

    expect_invalid(tmp_path, document, "scenario.start_time_utc", "ISO-8601")


# --------------------------------------------------------------------------- #
# Fault events
# --------------------------------------------------------------------------- #


def test_event_interval_must_stay_within_total_points(tmp_path: Path) -> None:
    """``[start, start + duration)`` must stay inside the run."""
    document = base_document()
    document["fault_generation"]["events"][0]["start_seq"] = 999_900
    document["fault_generation"]["events"][0]["duration_points"] = 512

    expect_invalid(
        tmp_path,
        document,
        "fault_generation.events[0]",
        "exceeds scenario.total_points",
    )


def test_event_start_must_be_non_negative(tmp_path: Path) -> None:
    """``seq`` starts at zero by contract."""
    document = base_document()
    document["fault_generation"]["events"][0]["start_seq"] = -1

    expect_invalid(tmp_path, document, "fault_generation.events[0].start_seq")


def test_event_duration_must_be_positive(tmp_path: Path) -> None:
    """A zero-length event covers nothing."""
    document = base_document()
    document["fault_generation"]["events"][0]["duration_points"] = 0

    expect_invalid(tmp_path, document, "fault_generation.events[0].duration_points")


def test_unsupported_fault_type_is_rejected(tmp_path: Path) -> None:
    """Only spike, step and burst are defined."""
    document = base_document()
    document["fault_generation"]["events"][0]["type"] = "glitch"

    expect_invalid(tmp_path, document, "fault_generation.events[0].type")


def test_duplicate_event_ids_are_rejected(tmp_path: Path) -> None:
    """Two events sharing an identifier would corrupt the truth file."""
    document = base_document()
    duplicate = dict(document["fault_generation"]["events"][0])
    document["fault_generation"]["events"].append(duplicate)

    expect_invalid(
        tmp_path,
        document,
        "fault_generation.events[1].event_id",
        "duplicate event_id",
    )


# --------------------------------------------------------------------------- #
# Consumer configuration
# --------------------------------------------------------------------------- #


def test_consumer_rejects_fault_information(tmp_path: Path) -> None:
    """The consumer document must never carry a fault schedule."""
    document = consumer_document(tmp_path)
    document["fault_generation"] = base_document()["fault_generation"]

    expect_invalid(
        tmp_path,
        document,
        "fault_generation",
        "unknown field",
        name="consumer.yaml",
        loader=load_consumer_config,
    )


def test_consumer_rejects_unknown_fields(tmp_path: Path) -> None:
    """Even a plausible producer key is unknown on the consumer side."""
    document = consumer_document(tmp_path)
    document["chunk_points"] = 102_400

    expect_invalid(
        tmp_path,
        document,
        "chunk_points",
        "unknown field",
        name="consumer.yaml",
        loader=load_consumer_config,
    )


def test_zero_window_points_is_rejected(tmp_path: Path) -> None:
    """A window must contain at least one sample."""
    document = consumer_document(tmp_path)
    document["window_points"] = 0

    expect_invalid(
        tmp_path,
        document,
        "window_points",
        "must be positive",
        name="consumer.yaml",
        loader=load_consumer_config,
    )


def test_incomplete_window_policy_is_checked(tmp_path: Path) -> None:
    """Only the documented policies are accepted."""
    document = consumer_document(tmp_path)
    document["incomplete_window"] = "skip"

    expect_invalid(
        tmp_path,
        document,
        "incomplete_window",
        "must be one of",
        name="consumer.yaml",
        loader=load_consumer_config,
    )


def test_missing_health_rules_file_is_reported(tmp_path: Path) -> None:
    """A dangling rules path must fail at load time, not at scoring time."""
    document = consumer_document(tmp_path)
    document["health_rules_file"] = "configs/does_not_exist.yaml"

    expect_invalid(
        tmp_path,
        document,
        "health_rules_file",
        "file not found",
        name="consumer.yaml",
        loader=load_consumer_config,
    )


def test_health_rules_path_is_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolved path must not depend on the working directory."""
    document = consumer_document(tmp_path)
    path = write_document(tmp_path, document, name="consumer.yaml")

    first = load_consumer_config(path).health_rules_path
    monkeypatch.chdir(tmp_path.parent)
    second = load_consumer_config(path).health_rules_path

    assert first == second
    assert first.is_absolute()


def test_health_rules_path_is_absolute_for_a_relative_config_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative ``--config`` argument must still yield an absolute path.

    The CLI is normally invoked from the repository root with
    ``--config configs/consumer.yaml``. The resulting object must not embed a
    relative path, otherwise it stops resolving as soon as the process changes
    its working directory.
    """
    monkeypatch.chdir(ROOT)

    config = load_consumer_config(Path("configs", "consumer.yaml"))

    assert config.health_rules_path.is_absolute()
    assert config.health_rules_path == (CONFIGS / "health_rules.yaml").resolve()
    assert config.health_rules_path.is_file()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_validate_succeeds_on_default_config(capsys: pytest.CaptureFixture) -> None:
    """The acceptance command must exit 0 and print a short summary."""
    code = main(["validate", "--config", str(CONFIGS / "default.yaml")])

    captured = capsys.readouterr()
    assert code == 0
    assert "Configuration OK" in captured.out
    assert captured.err == ""


def test_cli_validate_returns_2_and_names_the_field(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """An invalid document exits 2 and the field path goes to stderr."""
    document = base_document()
    document["scenario"]["sample_rate_hz"] = "100"
    path = write_document(tmp_path, document)

    code = main(["validate", "--config", str(path)])

    captured = capsys.readouterr()
    assert code == 2
    assert "scenario.sample_rate_hz" in captured.err


def test_cli_validate_returns_2_for_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A missing file is a user error, not a crash."""
    code = main(["validate", "--config", str(tmp_path / "gone.yaml")])

    captured = capsys.readouterr()
    assert code == 2
    assert "gone.yaml" in captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor"],
        [
            "simulate",
            "--config",
            str(CONFIGS / "default.yaml"),
            "--output-root",
            "runs",
        ],
    ],
)
def test_cli_unimplemented_subcommands_exit_non_zero(
    argv: list[str], capsys: pytest.CaptureFixture
) -> None:
    """Registered but unimplemented commands must never look successful."""
    code = main(argv)

    captured = capsys.readouterr()
    assert code != 0
    assert "not implemented" in captured.err


def test_cli_requires_a_subcommand() -> None:
    """Calling the CLI without a command is an argument error."""
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == 2


def test_cli_help_exits_zero() -> None:
    """``--help`` must be usable non-interactively."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0


def test_module_entrypoint_exit_codes(tmp_path: Path) -> None:
    """``python -m g15 validate`` must behave like ``main`` in a real process."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")

    ok = subprocess.run(
        [
            sys.executable,
            "-m",
            "g15",
            "validate",
            "--config",
            str(CONFIGS / "default.yaml"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr

    document = base_document()
    document["scenario"]["seed"] = True
    bad_path = write_document(tmp_path, document)
    bad = subprocess.run(
        [sys.executable, "-m", "g15", "validate", "--config", str(bad_path)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 2
    assert "scenario.seed" in bad.stderr
