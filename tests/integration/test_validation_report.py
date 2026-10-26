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


def test_report_uses_the_window_width_read_from_the_database(tmp_path: Path) -> None:
    """A run consumed at W=64 must be judged on W=64, not on the default 256.

    ``docs/interface.md`` section 9 stores no window-width column, so the only
    honest source is the geometry of the stored windows.  A report that
    assumed the acceptance width would widen every allowed alarm window by 4x
    and turn a miss into a detection (review of Issue #9, R1).

    The fixture is explicit: one event over ``[0, 64)``, and the only alarm row
    is at 256.  On the real grid W=64 the allowed range is ``[0, 192)``, so the
    alarm is a false alarm and the event is missed.  On the wrong grid W=256
    the range becomes ``[0, 768)`` and the same alarm would be scored as a
    detection.  The consumer's own alarm rows are removed first so the case
    measures the grid and nothing else.
    """
    import sqlite3  # noqa: PLC0415

    run_dir = _single_shard_run(
        tmp_path, total_points=10240, event_start=0, event_duration=64
    )
    database = run_dir / "artifacts" / "results.sqlite"
    consume_run(
        run_dir,
        database,
        _consumer_config(tmp_path, window_points=64),
    )
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("DELETE FROM alarms")
        connection.commit()
    finally:
        connection.close()
    _insert_alarm(database, run_dir, start_seq=256)

    payload = report_of(run_dir, database)

    assert payload["analysis_window_points"] == 64
    assert payload["feature_windows"] == 160
    assert payload["events"][0]["allowed_start"] == 0
    assert payload["events"][0]["allowed_end_exclusive"] == 192
    assert payload["events"][0]["detected"] is False
    assert payload["detected_events"] == 0
    assert payload["false_alarm_count"] == 1
    assert payload["passed"] is False


def test_report_rejects_a_run_whose_windows_disagree_on_width(tmp_path: Path) -> None:
    """Mixed window widths mean the grid cannot be trusted, so refuse to judge."""
    import sqlite3  # noqa: PLC0415

    from g15.reporting.builder import ReportError  # noqa: PLC0415

    run_dir, database = run_profile(tmp_path, "spike", "seed42")
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            "UPDATE feature_windows SET end_seq_exclusive = end_seq_exclusive + 1 "
            "WHERE rowid = (SELECT MIN(rowid) FROM feature_windows)"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ReportError, match="do not share one width"):
        build_report(run_dir, database)


def test_report_does_not_read_another_runs_database(tmp_path: Path) -> None:
    """Pairing a run with a different database must not become a pass.

    ``build_report`` receives both paths and cannot assume they belong
    together.  The ``runs`` row is the join: without it every count is zero
    because nothing was consumed *there*, and a report that called that a pass
    would let an unconsumed run claim acceptance (review of Issue #9, R3).
    """
    run_dir, _ = run_profile(tmp_path / "a", "spike", "seed42")
    other_dir, other_database = run_profile(tmp_path / "b", "healthy", "seed42")

    payload = report_of(run_dir, other_database)

    assert payload["verifiable"] is False
    assert payload["passed"] is None
    assert payload["scoring_version"] is None
    assert payload["alarm_count"] is None
    assert payload["detection_rate"] is None
    assert any("no runs row" in reason for reason in payload["verdict_reasons"])
    assert other_dir != run_dir


def test_report_reads_scoring_version_from_the_supplied_database(
    tmp_path: Path,
) -> None:
    """The scoring version must come from the same database as the windows."""
    import sqlite3  # noqa: PLC0415

    run_dir, database = run_profile(tmp_path, "spike", "seed42")
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("UPDATE runs SET scoring_version = 'probe-v9'")
        connection.commit()
    finally:
        connection.close()

    payload = report_of(run_dir, database)

    assert payload["scoring_version"] == "probe-v9"


def test_report_treats_a_missing_truth_file_as_unverifiable(tmp_path: Path) -> None:
    """Deleting the truth must not turn a failed fault run into a pass.

    The producer writes no truth file when it injected nothing, so absence is
    only readable when the run log says zero events.  A fault run whose file
    was deleted keeps ``fault_events == 1`` in its log, and the report must say
    the run cannot be judged instead of silently scoring ``normal`` (A review
    of Issue #9, R2).
    """
    run_dir, database = run_profile(tmp_path, "spike", "seed42")
    truth = run_dir / "private_truth" / "faults.json"
    truth.unlink()

    payload = report_of(run_dir, database)

    assert payload["verifiable"] is False
    assert payload["passed"] is None
    assert payload["scenario_kind"] is None
    assert payload["detection_rate"] is None
    assert any("private_truth" in reason for reason in payload["verdict_reasons"])


def test_report_rejects_a_truth_file_from_another_run(tmp_path: Path) -> None:
    """A truth file copied between runs must be refused, not substituted."""
    import shutil as _shutil  # noqa: PLC0415

    from g15.reporting.builder import ReportError  # noqa: PLC0415

    run_dir, database = run_profile(tmp_path / "a", "spike", "seed42")
    donor_dir, _ = run_profile(tmp_path / "b", "step", "seed42")
    _shutil.copyfile(
        donor_dir / "private_truth" / "faults.json",
        run_dir / "private_truth" / "faults.json",
    )

    with pytest.raises(ReportError, match="belongs to run"):
        build_report(run_dir, database)


def test_report_rejects_a_truth_file_with_broken_json(tmp_path: Path) -> None:
    """A JSON syntax error must surface as the documented ReportError."""
    from g15.reporting.builder import ReportError  # noqa: PLC0415

    run_dir, database = run_profile(tmp_path, "spike", "seed42")
    truth = run_dir / "private_truth" / "faults.json"
    truth.write_text('{"run_id": "x", "events": [', encoding="utf-8")

    with pytest.raises(ReportError, match="invalid truth file"):
        build_report(run_dir, database)


def test_report_reports_analysed_and_dropped_points_separately(
    tmp_path: Path,
) -> None:
    """``processed_points`` alone hides the samples no window ever covered.

    A single shard of 10240 points consumed at W=7000 yields one window and
    drops 3240 samples.  Reporting only "10240 processed, 0 rejected" would
    claim full analysis coverage; the report must show the drop as well, and
    the three counts must add back up to ``total_points`` (A review of Issue
    #9, R4).
    """
    run_dir = _single_shard_run(
        tmp_path, total_points=10240, event_start=512, event_duration=64
    )
    database = run_dir / "artifacts" / "results.sqlite"
    consume_run(run_dir, database, _consumer_config(tmp_path, window_points=7000))

    payload = report_of(run_dir, database)

    assert payload["processed_points"] == 10240
    assert payload["analysed_points"] == 7000
    assert payload["dropped_points"] == 3240
    assert payload["rejected_points"] == 0
    assert payload["feature_windows"] == 1
    assert payload["analysis_window_points"] == 7000
    assert (
        payload["analysed_points"]
        + payload["dropped_points"]
        + payload["rejected_points"]
        == payload["total_points"]
    )
    markdown = (build_report(run_dir, database).parent / "summary.md").read_text(
        encoding="utf-8"
    )
    assert "3240" in markdown


def _consumer_config(tmp_path: Path, *, window_points: int):
    """Return the committed consumer configuration with one field overridden."""
    import dataclasses  # noqa: PLC0415

    config = load_consumer_config(CONSUMER_CONFIG)
    return dataclasses.replace(config, window_points=window_points)


def _single_shard_run(
    tmp_path: Path, *, total_points: int, event_start: int, event_duration: int
) -> Path:
    """Produce one single-shard run from the controlled spike profile.

    Args:
        tmp_path: Directory that receives the run.
        total_points: Sample count of the run; must not exceed ``chunk_points``
            so the run holds a single shard.
        event_start: Absolute start of the single injected event.
        event_duration: Event length in samples.

    Returns:
        The run directory.
    """
    source = (CONFIGS / "acceptance_spike_seed42.yaml").read_text(encoding="utf-8")
    text = (
        source.replace("id: acceptance_spike_seed42", "id: single_shard_probe")
        .replace("total_points: 65536", f"total_points: {total_points}")
        .replace("start_seq: 16384", f"start_seq: {event_start}")
        .replace("duration_points: 256", f"duration_points: {event_duration}")
    )
    config_path = tmp_path / "single_shard_probe.yaml"
    config_path.write_text(text, encoding="utf-8")
    summary = produce_run_detailed(load_config(config_path), tmp_path)
    return summary.run_dir


def _insert_alarm(database: Path, run_dir: Path, *, start_seq: int) -> None:
    """Insert one extra alarm row so a hand-picked grid can be judged.

    The stored windows of a W=64 run never alarm, so the only way to exercise
    "an alarm outside the allowed range" is to add the row the consumer would
    have written had a rule fired.  The row uses the run's own device id and
    the rule id of the committed rule set.
    """
    import sqlite3  # noqa: PLC0415

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            "INSERT INTO alarms "
            "(run_id, device_id, rule_id, start_seq, level, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                manifest["run_id"],
                manifest["device_id"],
                "vibration_abs",
                start_seq,
                "yellow",
                "probe",
                "2026-10-26T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()


# -- R3: a partially consumed run is not a pass -----------------------------


def test_report_refuses_a_partially_consumed_run(tmp_path: Path) -> None:
    """A run whose second shard is damaged must not be reported as a pass.

    This is the real failure path A reproduced: the producer publishes a
    complete run, ``consume_run`` commits the first shard, the second shard's
    digest no longer matches the manifest, and consumption aborts.  The
    database therefore holds a ``runs`` row, some windows and some alarms --
    but the run was never consumed as a whole.  Reporting ``passed=true`` off
    the committed prefix would certify a run whose data is half missing
    (review of Issue #9, R3).

    The test then repairs the shard and re-runs the consumer, and asserts the
    same report turns into a pass: the blocker must be about consumption
    completeness, not about a permanently unverifiable run.
    """
    run_dir, database = run_profile(tmp_path, "spike", "seed42")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    # Damage the second shard *after* the first pass proved the run is
    # consumable, then consume into a fresh database so the abort happens
    # inside a single ``consume_run`` call.
    second = run_dir / manifest["parts"][1]["path"]
    good_bytes = second.read_bytes()
    second.write_bytes(good_bytes[:-8] + b"deadbeef")

    fresh = tmp_path / "partial" / "results.sqlite"
    fresh.parent.mkdir(parents=True)
    with pytest.raises(Exception):
        consume_run(run_dir, fresh, load_consumer_config(CONSUMER_CONFIG))

    payload = report_of(run_dir, fresh)
    assert payload["verifiable"] is False
    assert payload["passed"] is None
    assert payload["pending_parts"] == [1, 2, 3, 4, 5, 6]
    assert payload["processed_points"] == 10240
    assert payload["rejected_points"] == 65536 - 10240
    assert any("never finished this run" in r for r in payload["verdict_reasons"])

    # Repair and re-consume: the same report must now be a pass, so the
    # blocker is about completeness rather than about the fixture.
    second.write_bytes(good_bytes)
    consume_run(run_dir, fresh, load_consumer_config(CONSUMER_CONFIG))
    repaired = report_of(run_dir, fresh)
    assert repaired["pending_parts"] == []
    assert repaired["verifiable"] is True
    assert repaired["passed"] is True
    assert repaired["processed_points"] == 65536
    assert repaired["rejected_points"] == 0
    # The original database is untouched by any of this.
    assert report_of(run_dir, database)["passed"] is True


def test_report_refuses_a_run_whose_recorded_digest_was_swapped(
    tmp_path: Path,
) -> None:
    """A shard consumed from other bytes cannot be scored as this run.

    ``processed_chunks.sha256`` is the consumer's own record of what it read.
    When it disagrees with the manifest, the rows already stored for that part
    belong to different data, so a report that scores them would attribute
    somebody else's samples to this run (review of Issue #9, R3).
    """
    import sqlite3  # noqa: PLC0415

    run_dir, database = run_profile(tmp_path, "spike", "seed42")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            "UPDATE processed_chunks SET sha256 = ? WHERE part_id = 3",
            ("0" * 64,),
        )
        connection.commit()
    finally:
        connection.close()

    payload = report_of(run_dir, database)
    assert payload["verifiable"] is False
    assert payload["passed"] is None
    assert payload["digest_mismatch_parts"] == [3]
    assert payload["processed_points"] == 65536 - int(manifest["parts"][3]["rows"])
    assert any("different digest" in r for r in payload["verdict_reasons"])
