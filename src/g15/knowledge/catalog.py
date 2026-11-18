"""Validated packaged knowledge and traceable matches, with no file/DB mutation."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
import json
import math
import re
from typing import Mapping

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_MEASUREMENT = re.compile(
    rf"^\s*([a-z][a-z0-9_]*)=({_NUMBER})\s*\(risk\s+({_NUMBER})\)(?:\s+<=\s+(?:red|yellow) threshold {_NUMBER})?\s*$",
    re.I,
)


def validate_catalog(document: object) -> dict:
    """Validate the course-only knowledge schema; reject ambiguous entries."""
    if not isinstance(document, dict) or document.get("schema_version") != "1.0":
        raise ValueError("unsupported maintenance knowledge schema")
    for key in ("version", "scope", "source_id", "source"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise ValueError(f"missing knowledge {key}")
    if document.get("example_only") is not True:
        raise ValueError("this catalog must remain explicitly course-only")
    entries = document.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
        raise ValueError("invalid knowledge entries")
    ids, features = set(), set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid knowledge entry")
        for key in ("id", "feature", "action", "basis"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ValueError(f"missing entry {key}")
        if entry["id"] in ids or entry["feature"] in features:
            raise ValueError("duplicate knowledge identity or feature")
        ids.add(entry["id"])
        features.add(entry["feature"])
    # No shared mutable caller-owned objects escape validation.
    return json.loads(json.dumps(document, ensure_ascii=False, allow_nan=False))


@lru_cache(maxsize=1)
def _catalog_bytes() -> str:
    return (
        resources.files("g15.knowledge")
        .joinpath("maintenance.json")
        .read_text(encoding="utf-8")
    )


def load_catalog() -> dict:
    """Read versioned packaged rules; callers receive isolated validated copies."""
    return validate_catalog(json.loads(_catalog_bytes()))


def match_knowledge(alarm: Mapping, catalog: dict | None = None) -> tuple[dict, ...]:
    """Match positive public driver risks, retaining source alarm and rule evidence.

    Reads neither fault truth nor equipment state. Invalid/unknown measurements
    do not produce a diagnosis. Source reason is a legacy rounded explanation,
    not a substitute for exact feature values or a measured confidence score.
    """
    book = load_catalog() if catalog is None else validate_catalog(catalog)
    if (
        alarm.get("level") not in ("yellow", "red")
        or not isinstance(alarm.get("rule_id"), str)
        or type(alarm.get("start_seq")) is not int
        or alarm["start_seq"] < 0
    ):
        return ()
    reason = alarm.get("reason")
    if not isinstance(reason, str):
        return ()
    drivers = re.search(r"(?:^|;\s*)drivers:\s*([^;]+)", reason)
    if not drivers:
        return ()
    entries = {row["feature"]: row for row in book["entries"]}
    measurements = {}
    for part in drivers[1].split(","):
        match = _MEASUREMENT.fullmatch(part)
        if not match:
            continue
        feature, value, risk = match.groups()
        feature, value, risk = feature.lower(), float(value), float(risk)
        if (
            feature not in entries
            or not math.isfinite(value)
            or not math.isfinite(risk)
            or not 0 <= risk <= 1
        ):
            continue
        measurements[feature] = (value, risk)
    matches = []
    for feature, (value, risk) in measurements.items():
        if risk == 0:
            continue
        matches.append(
            {
                "entry_id": entries[feature]["id"],
                "knowledge_version": book["version"],
                "source_id": book["source_id"],
                "source": book["source"],
                "example_only": True,
                "action": entries[feature]["action"],
                "basis": entries[feature]["basis"],
                "evidence": {
                    "rule_id": alarm["rule_id"],
                    "start_seq": alarm["start_seq"],
                    "level": alarm["level"],
                    "feature": feature,
                    "value": value,
                    "risk": risk,
                    "reason": reason,
                },
            }
        )
    return tuple(matches)


def describe_match(match: Mapping) -> str:
    """Render advice with visible source/version rather than an opaque action."""
    return (
        f'{match["action"]} [知识 {match["entry_id"]} / '
        f'{match["knowledge_version"]} / {match["source_id"]}；课程示例]'
    )
