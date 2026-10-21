"""Unit tests for health scoring and the rule loader (Issue #7, owner B2).

Two kinds of evidence live here:

* **hand computed scores** — a single rule with weight 1.0 and a feature halfway
  between baseline and tolerance must score exactly 50, so the formula of
  section 8.3 is checked against arithmetic rather than against itself;
* **rejections** — weights that do not sum to one, inverted thresholds, unknown
  fields and missing features must fail with the offending field path, because a
  rule set that is silently ignored is worse than no rule set at all.

The shipped ``configs/health_rules.yaml`` is loaded once as well: a rule file
that the code cannot read is a broken delivery even if every other test passes.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import pytest

from g15.analysis.features import FeatureRecord
from g15.analysis.health import (
    SCHEMA_VERSION,
    HealthRulesError,
    load_health_rules,
    parse_health_rules,
    score_health,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "configs" / "health_rules.yaml"


def make_features(**overrides: float) -> FeatureRecord:
    """Return a healthy-looking record with selected features overridden."""
    values = {
        "mean": 0.5,
        "std": 0.05,
        "rms": 0.5025,
        "ptp": 0.282,
        "kurtosis_excess": 0.0,
        "crest_factor": 1.27,
        "dominant_freq_hz": 25.0,
        "zero_crossing_rate": 0.5,
    }
    values.update(overrides)
    return FeatureRecord(
        **values,
        sample_count=256,
        sample_rate_hz=100,
        window_start_seq=0,
    )


def make_document(**overrides: object) -> dict:
    """Return a minimal valid rule document, with selected parts overridden."""
    document: dict = {
        "schema_version": SCHEMA_VERSION,
        "scoring_version": "test-v1",
        "rule_id": "test_rule",
        "thresholds": {
            "yellow": 70.0,
            "red": 40.0,
            "consecutive_windows": 1,
            "recovery_windows": 1,
        },
        "features": [
            {
                "name": "rms",
                "baseline": 0.5,
                "tolerance": 0.5,
                "weight": 1.0,
                "direction": "increase",
            }
        ],
    }
    document.update(overrides)
    return document


def test_halfway_to_tolerance_scores_exactly_fifty() -> None:
    """risk = (0.75 - 0.5) / 0.5 = 0.5, so health = 100 * (1 - 1.0 * 0.5) = 50."""
    rules = parse_health_rules(make_document())
    result = score_health(make_features(rms=0.75), rules)

    assert result.health_score == pytest.approx(50.0)
    assert result.contributions[0].risk == pytest.approx(0.5)
    assert result.contributions[0].weighted_risk == pytest.approx(0.5)
    assert result.scoring_version == "test-v1"
    assert result.rule_id == "test_rule"


def test_at_or_below_baseline_scores_one_hundred() -> None:
    """A feature at or under its baseline contributes no risk."""
    rules = parse_health_rules(make_document())
    assert score_health(make_features(rms=0.5), rules).health_score == 100.0
    assert score_health(make_features(rms=0.1), rules).health_score == 100.0


def test_risk_saturates_at_tolerance_and_clips_to_zero() -> None:
    """Beyond the tolerance the score stays at 0 instead of going negative."""
    rules = parse_health_rules(make_document())
    assert score_health(make_features(rms=1.0), rules).health_score == 0.0
    assert score_health(make_features(rms=99.0), rules).health_score == 0.0


def test_score_is_monotonic_in_the_risky_feature() -> None:
    """A worse feature must never score better (no inverted risk direction)."""
    rules = parse_health_rules(make_document())
    scores = [
        score_health(make_features(rms=value), rules).health_score
        for value in (0.50, 0.60, 0.70, 0.80, 0.90, 1.00)
    ]
    assert scores == sorted(scores, reverse=True)


def test_two_sided_direction_penalises_both_directions() -> None:
    """A step fault can shift the level down as well as up."""
    document = make_document()
    document["features"] = [
        {
            "name": "mean",
            "baseline": 0.5,
            "tolerance": 0.5,
            "weight": 1.0,
            "direction": "two_sided",
        }
    ]
    rules = parse_health_rules(document)

    assert score_health(make_features(mean=1.0), rules).health_score == pytest.approx(
        0.0
    )
    assert score_health(make_features(mean=0.0), rules).health_score == pytest.approx(
        0.0
    )
    assert score_health(make_features(mean=0.75), rules).health_score == pytest.approx(
        50.0
    )


def test_decrease_direction_penalises_a_drop() -> None:
    """A feature that should stay high is scored from below its baseline."""
    document = make_document()
    document["features"] = [
        {
            "name": "std",
            "baseline": 0.05,
            "tolerance": 0.05,
            "weight": 1.0,
            "direction": "decrease",
        }
    ]
    rules = parse_health_rules(document)

    assert score_health(make_features(std=0.05), rules).health_score == 100.0
    assert score_health(make_features(std=0.0), rules).health_score == 0.0


def test_weights_are_applied_individually() -> None:
    """Two features, weights 0.3/0.7: 30 % + 70 % of full risk gives 0.0."""
    document = make_document()
    document["features"] = [
        {
            "name": "rms",
            "baseline": 0.5,
            "tolerance": 1.0,
            "weight": 0.3,
            "direction": "increase",
        },
        {
            "name": "ptp",
            "baseline": 0.3,
            "tolerance": 1.0,
            "weight": 0.7,
            "direction": "increase",
        },
    ]
    rules = parse_health_rules(document)

    both_full = score_health(make_features(rms=1.5, ptp=1.3), rules)
    assert both_full.health_score == pytest.approx(0.0)

    only_rms = score_health(make_features(rms=1.5, ptp=0.3), rules)
    assert only_rms.health_score == pytest.approx(70.0)

    only_ptp = score_health(make_features(rms=0.5, ptp=1.3), rules)
    assert only_ptp.health_score == pytest.approx(30.0)


def test_drivers_are_ranked_by_weighted_risk() -> None:
    """The explanation must name the features that actually removed points."""
    document = make_document()
    document["features"] = [
        {
            "name": "rms",
            "baseline": 0.5,
            "tolerance": 1.0,
            "weight": 0.5,
            "direction": "increase",
        },
        {
            "name": "crest_factor",
            "baseline": 1.3,
            "tolerance": 1.0,
            "weight": 0.5,
            "direction": "increase",
        },
    ]
    rules = parse_health_rules(document)
    result = score_health(make_features(rms=1.0, crest_factor=2.3), rules)

    # rms risk 0.5 at weight 0.5 plus crest risk 1.0 at weight 0.5 removes 75 points.
    assert result.drivers == ("crest_factor", "rms")
    assert "crest_factor=" in result.explain()
    assert result.health_score == pytest.approx(25.0)


def test_drivers_are_empty_when_nothing_is_above_baseline() -> None:
    """A perfect window has no driver, and ``explain`` says so."""
    rules = parse_health_rules(make_document())
    result = score_health(make_features(rms=0.1), rules)

    assert result.drivers == ()
    assert "no feature above baseline" in result.explain()


def test_missing_feature_fails_loudly() -> None:
    """A rule naming a feature the record cannot provide must not score 100."""

    class Partial:
        """Record-like object that only carries ``std``."""

        window_start_seq = 0

        @staticmethod
        def feature_value(name: str) -> float:
            if name == "std":
                return 0.05
            raise KeyError(name)

    rules = parse_health_rules(make_document())
    with pytest.raises(HealthRulesError, match="features.rms"):
        score_health(Partial(), rules)  # type: ignore[arg-type]


def test_non_finite_feature_fails_loudly() -> None:
    """NaN must not be scored as if it were healthy."""
    rules = parse_health_rules(make_document())
    with pytest.raises(HealthRulesError, match="not finite"):
        score_health(make_features(rms=math.nan), rules)


def test_window_position_is_carried_into_the_result() -> None:
    """Alarms need the window position, so scoring must not drop it."""
    rules = parse_health_rules(make_document())
    features = dataclasses.replace(make_features(), window_start_seq=4096)
    assert score_health(features, rules).window_start_seq == 4096


def test_shipped_rule_file_loads_and_is_consistent() -> None:
    """The delivered YAML must load, sum to one and cover all eight features."""
    rules = load_health_rules(RULES_PATH)

    assert rules.schema_version == SCHEMA_VERSION
    assert rules.weight_total == pytest.approx(1.0)
    assert rules.red_threshold < rules.yellow_threshold
    assert rules.consecutive_windows >= 1
    assert rules.recovery_windows >= 1
    assert len(rules.feature_rules) == 8

    weighted = [rule for rule in rules.feature_rules if rule.weight > 0.0]
    assert len(weighted) >= 5


def test_shipped_rule_file_keeps_the_acceptance_thresholds_reachable() -> None:
    """A healthy record must score above yellow with the shipped rules."""
    rules = load_health_rules(RULES_PATH)
    healthy = score_health(make_features(), rules)

    assert healthy.health_score > rules.yellow_threshold


@pytest.mark.parametrize(
    ("mutate", "expected_path"),
    [
        (lambda doc: doc["features"][0].update(weight=0.9), "features.weight"),
        (lambda doc: doc["features"][0].update(name="rms_db"), "features[0].name"),
        (lambda doc: doc["features"][0].update(tolerance=0.0), "features[0].tolerance"),
        (lambda doc: doc["features"][0].update(weight=-0.5), "features[0].weight"),
        (
            lambda doc: doc["features"][0].update(direction="upwards"),
            "features[0].direction",
        ),
        (
            lambda doc: doc["features"][0].update(baseline="high"),
            "features[0].baseline",
        ),
        (lambda doc: doc["features"][0].update(weight=True), "features[0].weight"),
        (lambda doc: doc["thresholds"].update(red=80.0), "thresholds.red"),
        (lambda doc: doc["thresholds"].update(yellow=0.0), "thresholds.yellow"),
        (
            lambda doc: doc["thresholds"].update(consecutive_windows=0),
            "thresholds.consecutive_windows",
        ),
        (
            lambda doc: doc["thresholds"].update(recovery_windows=0),
            "thresholds.recovery_windows",
        ),
        (lambda doc: doc.update(schema_version="2.0"), "schema_version"),
        (lambda doc: doc.update(scoring_version=""), "scoring_version"),
        (lambda doc: doc.pop("rule_id"), "rule_id"),
        (lambda doc: doc.update(weights=[1.0]), "weights"),
        (lambda doc: doc["features"][0].update(wieght=1.0), "features[0].wieght"),
        (
            lambda doc: doc["features"].append(dict(doc["features"][0], weight=0.0)),
            "features[1].name",
        ),
        (lambda doc: doc.update(features=[]), "features"),
        (lambda doc: doc["thresholds"].update(red="high"), "thresholds.red"),
    ],
)
def test_invalid_documents_are_rejected_with_a_path(mutate, expected_path: str) -> None:
    """Every rejection must name the field, otherwise the operator cannot fix it."""
    document = make_document()
    mutate(document)
    with pytest.raises(HealthRulesError) as error:
        parse_health_rules(document)
    assert error.value.path == expected_path
    assert error.value.reason


def test_duplicate_feature_names_are_rejected() -> None:
    """Two rules for the same feature would double count its risk."""
    document = make_document()
    document["features"] = [dict(document["features"][0]) for _ in range(2)]
    document["features"][1]["weight"] = 0.0
    with pytest.raises(HealthRulesError, match="duplicate feature"):
        parse_health_rules(document)


def test_non_mapping_document_is_rejected() -> None:
    """A list is not a rule document."""
    with pytest.raises(HealthRulesError, match="must be a mapping"):
        parse_health_rules([{"schema_version": SCHEMA_VERSION}])


def test_missing_file_is_reported_as_a_path_error(tmp_path: Path) -> None:
    """A missing rule file must name the file, not raise ``FileNotFoundError``."""
    missing = tmp_path / "nope.yaml"
    with pytest.raises(HealthRulesError, match="file not found"):
        load_health_rules(missing)


def test_invalid_yaml_is_reported_as_a_path_error(tmp_path: Path) -> None:
    """Broken YAML must be a rule error so the CLI can exit with code 2."""
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        "schema_version: '1.0'\nthresholds: [unclosed\n", encoding="utf-8"
    )
    with pytest.raises(HealthRulesError, match="invalid YAML"):
        load_health_rules(broken)


def test_load_accepts_a_string_path(tmp_path: Path) -> None:
    """Callers may pass the configured string instead of a ``Path``."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        "\n".join(
            [
                "schema_version: '1.0'",
                "scoring_version: 'test-v1'",
                "rule_id: 'test_rule'",
                "thresholds:",
                "  yellow: 70.0",
                "  red: 40.0",
                "  consecutive_windows: 1",
                "  recovery_windows: 1",
                "features:",
                "  - name: rms",
                "    baseline: 0.5",
                "    tolerance: 0.5",
                "    weight: 1.0",
                "    direction: increase",
            ]
        ),
        encoding="utf-8",
    )
    rules = load_health_rules(str(path))
    assert rules.rule_id == "test_rule"
