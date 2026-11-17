"""Course knowledge provenance, conservative matching and frontend parity."""

import json
from pathlib import Path

import pytest

from g15.knowledge.catalog import load_catalog, match_knowledge, validate_catalog
from g15.notifications.advice import maintenance_advice

ROOT = Path(__file__).resolve().parents[2]


def alarm(reason="health 32.00; drivers: rms=2 (risk 0.9)"):
    return dict(rule_id="vibration-v1", start_seq=256, level="red", reason=reason)


def test_traceable_match_and_legacy_advice():
    result = match_knowledge(alarm())
    assert len(result) == 1
    assert result[0]["entry_id"] == "KB-003"
    assert result[0]["example_only"] is True
    assert result[0]["source_id"] == "G15-EXAMPLE-001"
    assert result[0]["evidence"] == {
        "rule_id": "vibration-v1",
        "start_seq": 256,
        "level": "red",
        "feature": "rms",
        "value": 2.0,
        "risk": 0.9,
        "reason": alarm()["reason"],
    }
    text = maintenance_advice([alarm()], [])
    assert any("KB-003" in item and "课程示例" in item for item in text)
    assert any("历史告警" in item for item in text)


@pytest.mark.parametrize(
    "reason",
    [
        "health 1",
        "drivers: rms=Inf (risk 0.9)",
        "drivers: rms=1 (risk 0)",
        "drivers: rms=1 (risk -0.1)",
        "drivers: rms=1 (risk 1.1)",
        "drivers: unknown=1 (risk 0.5)",
        "drivers: rms=1 (risk 0.5)garbage",
    ],
)
def test_no_invention_from_score_or_bad_evidence(reason):
    assert match_knowledge(alarm(reason)) == ()


def test_duplicate_drivers_and_unknown_do_not_duplicate_advice():
    result = match_knowledge(
        alarm("drivers: rms=2 (risk 0.8), rms=2 (risk 0.8), x=9 (risk 1)")
    )
    assert len(result) == 1


@pytest.mark.parametrize(
    "field,value",
    [("level", "normal"), ("start_seq", -1), ("start_seq", True), ("rule_id", None)],
)
def test_invalid_source_is_rejected(field, value):
    row = alarm()
    row[field] = value
    assert match_knowledge(row) == ()


def test_catalog_validation_and_mutation_isolation():
    book = load_catalog()
    book["entries"][0]["action"] = "changed"
    assert load_catalog()["entries"][0]["action"] != "changed"
    book["entries"].append(book["entries"][0])
    with pytest.raises(ValueError, match="duplicate"):
        validate_catalog(book)
    book = load_catalog()
    book["example_only"] = False
    with pytest.raises(ValueError, match="course-only"):
        validate_catalog(book)


def test_frontend_uses_identical_packaged_catalog():
    generated = (ROOT / "src/g15/console/static/maintenance-catalog.js").read_text(
        encoding="utf-8"
    )
    data = json.loads(
        generated.split("export const maintenanceCatalog = ", 1)[1].rstrip(";\n")
    )
    assert data == load_catalog()


def test_real_alarm_threshold_suffix_is_preserved():
    row = alarm("health 30; drivers: rms=2 (risk 0.9) <= red threshold 40.00")
    assert match_knowledge(row)[0]["entry_id"] == "KB-003"


def test_duplicate_driver_uses_last_valid_measurement_like_browser():
    assert match_knowledge(alarm("drivers: rms=2 (risk 0.9), rms=0 (risk 0)")) == ()
    match = match_knowledge(alarm("drivers: rms=0 (risk 0), rms=2 (risk 0.9)"))[0]
    assert match["evidence"]["risk"] == 0.9
