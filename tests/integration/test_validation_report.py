"""Integration tests for the independent validator and report builder (#9).

These tests drive the real pipeline end to end: configuration -> producer ->
consumer -> SQLite -> validator -> ``reports/summary.json`` and ``.md``.  No
module is faked, because the point of the report is to describe what the
pipeline actually stored.

The controlled profiles of ``docs/acceptance.md`` section 3 are used as they
are committed: ``configs/acceptance_<kind>_<seed>.yaml``.  Running them here is
a regression on the committed configuration, not an independent validation
round -- see ``docs/test_plan.md`` for that distinction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from g15.config import load_config, load_consumer_config
from g15.pipeline.consumer import consume_run
from g15.pipeline.producer import produce_run_detailed
from g15.reporting.builder import REPORT_DIRECTORY, build_report
from g15.reporting.metrics import (
    SCENARIO_DEGRADATION,
    SCENARIO_FAULT,
    SCENARIO_NORMAL,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIGS = REPOSITORY_ROOT / "configs"
CONSUMER_CONFIG = CONFIGS / "consumer.yaml"

#: The six controlled fault profiles: three waveforms on two seeds.
FAULT_PROFILES = [
    (waveform, seed)
    for waveform in ("spike", "step", "burst")
    for seed in ("seed42", "seed20261016")
]

#: The two normal profiles and the two degradation profiles.
NORMAL_PROFILES = [("healthy", "seed42"), ("healthy", "seed20261016")]
DEGRADATION_PROFILES = [
    ("degradation", "seed42"),
    ("degradation", "seed20261016"),
]


def run_profile(tmp_path: Path, kind: str, seed: str) -> tuple[Path, Path]:
    """Generate and consume one committed acceptance profile.

    Args:
        tmp_path: Directory that receives the run.
        kind: Profile family, e.g. ``spike`` or ``healthy``.
        seed: Seed suffix, ``seed42`` or ``seed20261016``.

    Returns:
        ``(run_dir, database_path)``.
    """
    config = load_config(CONFIGS / f"acceptance_{kind}_{seed}.yaml")
    summary = produce_run_detailed(config, tmp_path)
    database = summary.run_dir / "artifacts" / "results.sqlite"
    consume_run(summary.run_dir, database, load_consumer_config(CONSUMER_CONFIG))
    return (summary.run_dir, database)


def report_of(run_dir: Path, database: Path) -> dict:
    """Build the report for a run and return the parsed JSON payload."""
    path = build_report(run_dir, database)
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("kind", "seed"), NORMAL_PROFILES)
def test_normal_profile_has_zero_alarms_and_null_rate(
    tmp_path: Path, kind: str, seed: str
) -> None:
    """A normal run must raise no alarm and report ``null``, not ``100%``."""
    run_dir, database = run_profile(tmp_path, kind, seed)
    payload = report_of(run_dir, database)

    assert payload["scenario_kind"] == SCENARIO_NORMAL
    assert payload["alarm_count"] == 0
    assert payload["false_alarm_count"] == 0
    assert payload["detection_rate"] is None
    assert payload["fault_events"] == 0
    assert payload["passed"] is True


@pytest.mark.parametrize(("kind", "seed"), DEGRADATION_PROFILES)
def test_degradation_profile_is_judged_by_trend_not_by_zero_alarms(
    tmp_path: Path, kind: str, seed: str
) -> None:
    """A degrading run may alarm; only its health trend decides the verdict.

    The alarm raised by the trend must be reported, must not be folded into the
    normal-scenario false-alarm gate, and must not fail the run.
    """
    run_dir, database = run_profile(tmp_path, kind, seed)
    payload = report_of(run_dir, database)

    assert payload["scenario_kind"] == SCENARIO_DEGRADATION
    assert payload["detection_rate"] is None
    assert payload["passed"] is True
    assert payload["health_score_max"] > payload["health_score_min"]
    assert payload["alarm_count"] >= 1
    assert payload["alarm_count"] == (
        payload["false_alarm_count"]
        + payload["redundant_alarm_count"]
        + payload["detected_events"]
    )


@pytest.mark.parametrize(("kind", "seed"), FAULT_PROFILES)
def test_fault_profiles_detect_their_single_event(
    tmp_path: Path, kind: str, seed: str
) -> None:
    """Every controlled fault profile must detect its one injected event."""
    run_dir, database = run_profile(tmp_path, kind, seed)
    payload = report_of(run_dir, database)

    assert payload["scenario_kind"] == SCENARIO_FAULT
    assert payload["fault_events"] == 1
    assert payload["detected_events"] == 1
    assert payload["missed_events"] == 0
    assert payload["detection_rate"] == 1.0
    assert payload["false_alarm_count"] == 0
    assert payload["passed"] is True
    assert payload["events"][0]["detected"] is True
    assert payload["events"][0]["matched_alarm_start"] is not None


def test_report_writes_both_files_with_the_same_numbers(tmp_path: Path) -> None:
    """The Markdown must carry the same numbers as the JSON."""
    run_dir, database = run_profile(tmp_path, "spike", "seed42")
    json_path = build_report(run_dir, database)
    markdown_path = json_path.with_name("summary.md")

    assert json_path.parent.name == REPORT_DIRECTORY
    assert json_path.is_file()
    assert markdown_path.is_file()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert f"| 注入事件总数 | {payload['fault_events']} |" in markdown
    assert f"| 已检出事件 | {payload['detected_events']} |" in markdown
    assert f"| 误报数 | {payload['false_alarm_count']} |" in markdown
    assert "100.00%" in markdown
    assert "Python" in markdown
    assert "双 Linux" in markdown


def test_report_is_reproducible_for_the_same_run(tmp_path: Path) -> None:
    """Rebuilding the report must produce byte-identical JSON."""
    run_dir, database = run_profile(tmp_path, "step", "seed42")
    first = build_report(run_dir, database).read_text(encoding="utf-8")
    second = build_report(run_dir, database).read_text(encoding="utf-8")
    assert first == second


def test_report_refuses_a_run_without_a_database(tmp_path: Path) -> None:
    """A run that was never consumed must fail loudly, not report zeros."""
    config = load_config(CONFIGS / "acceptance_spike_seed42.yaml")
    summary = produce_run_detailed(config, tmp_path)
    missing = summary.run_dir / "artifacts" / "results.sqlite"

    from g15.reporting.builder import ReportError  # noqa: PLC0415

    with pytest.raises(ReportError, match="no database"):
        build_report(summary.run_dir, missing)


def test_report_refuses_a_malformed_truth_file(tmp_path: Path) -> None:
    """A truth file that breaks section 5.3 must be rejected, not ignored."""
    run_dir, database = run_profile(tmp_path, "spike", "seed42")
    truth = run_dir / "private_truth" / "faults.json"
    document = json.loads(truth.read_text(encoding="utf-8"))
    document["events"][0]["start_seq"] = "16384"
    truth.write_text(json.dumps(document), encoding="utf-8")

    from g15.reporting.builder import ReportError  # noqa: PLC0415

    with pytest.raises(ReportError, match="invalid truth file"):
        build_report(run_dir, database)


def test_report_marks_rejected_points_when_a_shard_was_not_consumed(
    tmp_path: Path,
) -> None:
    """A partially consumed run must show the difference, not hide it.

    The consumer records one row per processed shard; dropping one of those
    rows is exactly the "some samples never reached the analysis" case that
    ``processed_points`` and ``rejected_points`` exist to expose.
    """
    import sqlite3  # noqa: PLC0415

    run_dir, database = run_profile(tmp_path, "spike", "seed42")
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            "DELETE FROM processed_chunks WHERE part_id = (SELECT MAX(part_id) FROM processed_chunks)"
        )
        connection.commit()
    finally:
        connection.close()

    payload = report_of(run_dir, database)
    assert payload["processed_points"] < payload["total_points"]
    assert payload["rejected_points"] == (
        payload["total_points"] - payload["processed_points"]
    )


def test_manifest_run_id_matches_the_report(tmp_path: Path) -> None:
    """The report must describe the run it was built from."""
    run_dir, database = run_profile(tmp_path, "burst", "seed20261016")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    payload = report_of(run_dir, database)

    assert payload["run_id"] == manifest["run_id"]
    assert payload["scenario_id"] == manifest["scenario_id"]
    assert payload["device_id"] == manifest["device_id"]
    assert payload["total_points"] == manifest["total_points"]
    assert payload["scalar_value_count"] == 2 * manifest["total_points"]
    assert payload["schema_version"] == manifest["schema_version"]
