"""Run a frozen ten-case engineering validation without selecting any seeds.

This tool is deliberately opt-in. A frozen manifest pins the source commit,
runner, consumer, rules and every exact configuration before the first CLI
process starts. Results are engineering evidence, not independent acceptance.
It never reads private truth; only the existing report CLI does that.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
from urllib.parse import urlparse

import yaml

PROFILES = ("normal", "degradation", "spike", "step", "burst")
COUNTS = (
    "fault_events",
    "detected_events",
    "missed_events",
    "alarm_count",
    "false_alarm_count",
    "redundant_alarm_count",
    "unmatched_alarm_count",
)


def digest(path: Path) -> str:
    """Hash exact file bytes, without newline conversion."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _git(source: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={source.as_posix()}", "-C", str(source), *args],
        text=True,
        encoding="utf-8",
    ).strip()


def _pinned_file(item: dict, base: Path) -> Path:
    _require(isinstance(item, dict), "file pin must be an object")
    _require(isinstance(item.get("path"), str), "file pin needs a path")
    path = (base / item["path"]).resolve()
    _require(path.is_file(), f"missing pinned file: {path}")
    _require(digest(path) == item.get("sha256"), f"file digest mismatch: {path}")
    return path


def validate_case_config(case: dict, path: Path, source: Path) -> None:
    """Require the unchanged reviewed profile, except identity and seed."""
    profile = case["profile"]
    template_name = "healthy" if profile == "normal" else profile
    template = yaml.safe_load(
        (source / "configs" / f"acceptance_{template_name}_seed42.yaml").read_text(
            encoding="utf-8"
        )
    )
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(isinstance(config, dict), "configuration must be a mapping")
    scenario = config.get("scenario", {})
    _require(isinstance(scenario, dict), "scenario must be a mapping")
    _require(scenario.get("seed") == case["seed"], "configuration seed mismatch")
    _require(type(scenario.get("seed")) is int, "configuration seed must be int")
    expected_kind = profile if profile in ("normal", "degradation") else None
    _require(scenario.get("kind") == expected_kind, "scenario kind mismatch")
    if expected_kind is None:
        _require("kind" not in scenario, "fault configurations must omit kind")
    for document in (config, template):
        for name in ("id", "description", "seed", "kind"):
            document["scenario"].pop(name, None)
    _require(config == template, "configuration changes the reviewed fixed profile")


def preflight(manifest_path: Path, source: Path) -> tuple[dict, list[Path], Path]:
    """Check frozen inputs without running a CLI or generating any data."""
    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8"), parse_constant=_invalid_json_constant
    )
    _require(isinstance(manifest, dict), "manifest must be an object")
    _require(manifest.get("format") == "g15-final-validation-v1", "unknown format")
    _require(manifest.get("status") == "frozen", "manifest is not frozen")
    _require(manifest.get("rules_frozen") is True, "rules are not frozen")
    receipt = manifest.get("authorization_receipt")
    _require(isinstance(receipt, str), "missing authorization receipt")
    url = urlparse(receipt)
    _require(
        url.scheme == "https" and bool(url.netloc) and not url.username,
        "authorization receipt must be an HTTPS URL",
    )
    timestamp = manifest.get("frozen_at_utc")
    _require(isinstance(timestamp, str), "missing freeze timestamp")
    frozen = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    _require(
        frozen.tzinfo is not None
        and frozen.utcoffset().total_seconds() == 0
        and frozen <= datetime.now(timezone.utc),
        "freeze timestamp must be past UTC",
    )
    commit = manifest.get("source_commit", "")
    _require(
        isinstance(commit, str) and bool(re.fullmatch(r"[0-9a-f]{40}", commit)),
        "invalid source commit",
    )
    _require(_git(source, "rev-parse", "HEAD") == commit, "source commit mismatch")
    _require(
        not _git(source, "status", "--porcelain", "--untracked-files=all"),
        "source has local changes or untracked files",
    )
    _require(
        Path(__file__).resolve()
        == (source / "tools/run_final_validation.py").resolve(),
        "runner must execute from the pinned source checkout",
    )
    _require(
        digest(Path(__file__)) == manifest.get("runner_sha256"),
        "runner digest mismatch",
    )
    consumer = _pinned_file(manifest["consumer_config"], manifest_path.parent)
    rules = _pinned_file(manifest["health_rules"], manifest_path.parent)
    _require(
        consumer == (source / "configs/consumer.yaml").resolve(),
        "use the committed consumer configuration",
    )
    _require(
        rules == (source / "configs/health_rules.yaml").resolve(),
        "use the committed health rules",
    )
    consumer_data = yaml.safe_load(consumer.read_text(encoding="utf-8"))
    _require(
        consumer_data
        == {
            "schema_version": "1.0",
            "window_points": 256,
            "incomplete_window": "drop",
            "health_rules_file": "configs/health_rules.yaml",
        },
        "consumer configuration differs from the fixed profile",
    )
    cases = manifest.get("cases")
    _require(isinstance(cases, list) and len(cases) == 10, "exactly ten cases required")
    matrix, seeds, paths = set(), {}, []
    for case in cases:
        _require(isinstance(case, dict), "case must be an object")
        slot, profile, seed = case.get("slot_id"), case.get("profile"), case.get("seed")
        _require(type(slot) is int and slot in (1, 2), "invalid slot")
        _require(profile in PROFILES, "invalid profile")
        _require(type(seed) is int and 1 <= seed <= 2**32 - 1, "invalid seed")
        _require((slot, profile) not in matrix, "duplicate case")
        _require(slot not in seeds or seeds[slot] == seed, "slot has multiple seeds")
        seeds[slot] = seed
        matrix.add((slot, profile))
        path = _pinned_file(case, manifest_path.parent)
        validate_case_config(case, path, source)
        paths.append(path)
    _require(
        matrix == {(slot, kind) for slot in (1, 2) for kind in PROFILES},
        "incomplete matrix",
    )
    _require(len(set(seeds.values())) == 2, "the two slots must have distinct seeds")
    _require(len(set(paths)) == 10, "each case requires a distinct configuration")
    return manifest, paths, consumer


def evaluate_report(profile: str, report: dict, scores: list[float]) -> dict:
    """Check complete report facts and compute the fixed degradation trend."""
    reasons = []
    if not isinstance(report, dict):
        return {
            "passed": False,
            "reasons": ["report must be an object"],
            "first_half_health_median": None,
            "second_half_health_median": None,
        }
    for name in COUNTS:
        value = report.get(name)
        if type(value) is not int or value < 0:
            reasons.append(f"missing or invalid {name}")
    kind = profile if profile in ("normal", "degradation") else "fault"
    if report.get("scenario_kind") != kind or report.get("verifiable") is not True:
        reasons.append("report kind or verifiability mismatch")
    if report.get("passed") is not True:
        reasons.append("report did not pass its own criterion")
    expected = 1 if kind == "fault" else 0
    if report.get("fault_events") != expected or report.get("missed_events") != 0:
        reasons.append("unexpected event or missed-event count")
    if report.get("detected_events") != expected:
        reasons.append("event detection count mismatch")
    if report.get("detection_rate") != (1.0 if expected else None):
        reasons.append("detection rate mismatch")
    events = report.get("events")
    if not isinstance(events, list) or len(events) != expected:
        reasons.append("missing per-event mappings")
    elif expected and (
        not isinstance(events[0], dict)
        or events[0].get("detected") is not True
        or events[0].get("type") != profile
        or type(events[0].get("matched_alarm_start")) is not int
    ):
        reasons.append("invalid per-event mapping")
    if profile == "normal" and (
        report.get("alarm_count") != 0 or report.get("false_alarm_count") != 0
    ):
        reasons.append("normal scenario raised an alarm")
    first = second = None
    if profile == "degradation":
        if (
            not isinstance(scores, list)
            or len(scores) != 256
            or any(
                type(value) not in (float, int)
                or not math.isfinite(value)
                or not 0 <= value <= 100
                for value in scores
            )
        ):
            reasons.append("missing or invalid health-score trace")
        else:
            first, second = statistics.median(scores[:128]), statistics.median(
                scores[128:]
            )
            if second >= first:
                reasons.append("degradation second-half median is not lower")
    if not reasons and (
        report["unmatched_alarm_count"]
        != report["false_alarm_count"] + report["redundant_alarm_count"]
        or report["alarm_count"]
        != report["detected_events"] + report["unmatched_alarm_count"]
    ):
        reasons.append("alarm accounting mismatch")
    for name, expected_value in (
        ("total_points", 65536),
        ("processed_points", 65536),
        ("analysed_points", 65536),
        ("analysis_window_points", 256),
        ("feature_windows", 256),
        ("rejected_points", 0),
        ("dropped_points", 0),
    ):
        if type(report.get(name)) is not int or report[name] != expected_value:
            reasons.append(f"point/window accounting mismatch: {name}")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "first_half_health_median": first,
        "second_half_health_median": second,
    }


def _write_json(path: Path, data: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class FrozenInputsChanged(ValueError):
    """A batch must stop when its original frozen material no longer matches."""


def _check_original_freeze(
    manifest_path: Path, source: Path, original_hash: str
) -> None:
    """Recheck the initial authority; a replacement freeze never authorizes continuation."""
    try:
        _require(digest(manifest_path) == original_hash, "frozen manifest changed")
        preflight(manifest_path, source)
        _require(
            digest(manifest_path) == original_hash,
            "frozen manifest changed during check",
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        yaml.YAMLError,
        subprocess.SubprocessError,
    ) as exc:
        raise FrozenInputsChanged(str(exc)) from exc


def execute(manifest_path: Path, source: Path, output: Path) -> dict:
    """Execute each frozen case once and preserve failures alongside successes."""
    frozen_bytes = manifest_path.read_bytes()
    original_hash = hashlib.sha256(frozen_bytes).hexdigest()
    manifest, configs, consumer = preflight(manifest_path, source)
    _require(
        digest(manifest_path) == original_hash,
        "frozen manifest changed during preflight",
    )
    _require(
        not output.is_relative_to(source),
        "evidence output must be outside the source checkout",
    )
    output.mkdir(parents=True, exist_ok=False)
    with (output / "frozen-input.json").open("xb") as stream:
        stream.write(frozen_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    metadata = {
        "evidence_kind": "engineering_validation",
        "independent_acceptance": False,
        "source_commit": manifest["source_commit"],
        "manifest_sha256": original_hash,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "cases": [],
        "aborted": False,
    }
    _write_json(output / "started.json", metadata)
    environment = dict(
        os.environ, PYTHONPATH=str(source / "src"), PYTHONNOUSERSITE="1", PYTHONUTF8="1"
    )
    for case, config in zip(manifest["cases"], configs):
        case_root = output / f"slot{case['slot_id']}-{case['profile']}"
        case_root.mkdir()
        result = {
            "slot_id": case["slot_id"],
            "profile": case["profile"],
            "seed": case["seed"],
            "commands": [],
            "passed": False,
        }
        run_dir = None
        try:
            _check_original_freeze(manifest_path, source, original_hash)
            for stage in ("validate", "simulate", "consume", "report"):
                _check_original_freeze(manifest_path, source, original_hash)
                args = [stage]
                if stage in ("validate", "simulate"):
                    args += ["--config", str(config)]
                else:
                    args += ["--run-dir", str(run_dir)]
                if stage == "simulate":
                    args += ["--output-root", str(case_root / "runs")]
                if stage == "consume":
                    args += ["--config", str(consumer)]
                command = [sys.executable, "-m", "g15", *args]
                with (
                    (case_root / f"{stage}.stdout.log").open("xb") as stdout,
                    (case_root / f"{stage}.stderr.log").open("xb") as stderr,
                ):
                    process = subprocess.run(
                        command,
                        cwd=source,
                        env=environment,
                        stdout=stdout,
                        stderr=stderr,
                        check=False,
                    )
                result["commands"].append(
                    {"argv": command, "exit_code": process.returncode}
                )
                _write_json(case_root / f"{stage}.command.json", result["commands"][-1])
                _check_original_freeze(manifest_path, source, original_hash)
                if stage == "simulate" and process.returncode == 0:
                    runs = list((case_root / "runs").glob("*/manifest.json"))
                    _require(len(runs) == 1, "simulate did not produce exactly one run")
                    run_dir = runs[0].parent
                if process.returncode != 0:
                    break
            report_path = run_dir / "reports/summary.json" if run_dir else None
            if report_path and report_path.is_file():
                report = json.loads(
                    report_path.read_text(encoding="utf-8"),
                    parse_constant=_invalid_json_constant,
                )
                scores = []
                if case["profile"] == "degradation":
                    database = run_dir / "artifacts/results.sqlite"
                    with sqlite3.connect(
                        database.as_uri() + "?mode=ro", uri=True
                    ) as connection:
                        scores = [
                            row[0]
                            for row in connection.execute(
                                "SELECT health_score FROM feature_windows WHERE run_id=? ORDER BY start_seq",
                                (report["run_id"],),
                            )
                        ]
                result["report"] = report
                result["evaluation"] = evaluate_report(case["profile"], report, scores)
                result["passed"] = (
                    result["evaluation"]["passed"]
                    and len(result["commands"]) == 4
                    and all(item["exit_code"] == 0 for item in result["commands"])
                    and (run_dir / "reports/summary.md").is_file()
                )
            else:
                result["error"] = "no summary report produced"
            _check_original_freeze(manifest_path, source, original_hash)
        except FrozenInputsChanged as exc:
            result["passed"] = False
            result["error"] = f"FrozenInputsChanged: {exc}"
            metadata["aborted"] = True
            metadata["abort_reason"] = result["error"]
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            sqlite3.Error,
            yaml.YAMLError,
            subprocess.SubprocessError,
        ) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        _write_json(case_root / "result.json", result)
        metadata["cases"].append(result)
        if metadata["aborted"]:
            break
    metadata["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    metadata["unstarted_cases"] = 10 - len(metadata["cases"])
    metadata["passed"] = len(metadata["cases"]) == 10 and all(
        case["passed"] for case in metadata["cases"]
    )
    _write_json(output / "result.json", metadata)
    return metadata


def main(argv: list[str] | None = None) -> int:
    """Validate frozen inputs by default; --execute is required to run data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.execute:
            _require(args.output is not None, "--execute requires --output")
            result = execute(
                args.manifest.resolve(), args.source.resolve(), args.output.resolve()
            )
            print(
                json.dumps(
                    {"passed": result["passed"], "independent_acceptance": False}
                )
            )
            return 0 if result["passed"] else 4
        preflight(args.manifest.resolve(), args.source.resolve())
        print("Frozen inputs verified; no data generated or executed.")
        return 0
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        yaml.YAMLError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"preflight refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
