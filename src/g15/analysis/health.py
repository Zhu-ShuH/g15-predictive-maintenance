"""Explainable heuristic health scoring (Issue #7, owner B2).

Implements ``docs/interface.md`` section 8.3::

    risk_i = clip((f_i - b_i) / r_i, 0, 1)
    health = clip(100 * (1 - sum(w_i * risk_i)), 0, 100)

with every ``b_i``, ``r_i`` and ``w_i`` read from ``configs/health_rules.yaml``.
The score is a *heuristic risk indicator*, not a remaining-useful-life estimate
and not a trained diagnostic model; the docstrings and the rule file repeat that
so nobody quotes it as prediction accuracy.

Three deliberate design points:

* **Weights must sum to one.** A rule file whose weights silently sum to 0.9
  would make every score incomparable with a previous run, so the loader rejects
  it instead of normalising it behind the operator's back.
* **A missing feature fails loudly.** If a rule names a feature the record does
  not carry, the score would be computed from a partial vector and quietly look
  healthy; :func:`score_health` raises instead.
* **Direction is explicit.** ``increase`` scores a rise, ``decrease`` a fall and
  ``two_sided`` the absolute deviation, which is what a step fault needs (its
  signature is a level shift in either direction, not a rise).

``load_health_rules`` is the only function here that touches the filesystem, and
it exists because ``g15.config`` deliberately leaves this file's content to B2
(see the module docstring of ``config.py``). Everything else is pure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping

import yaml

from g15.analysis.features import FEATURE_NAMES, FeatureRecord

__all__ = [
    "DIRECTIONS",
    "SCHEMA_VERSION",
    "HealthResult",
    "HealthRules",
    "HealthRulesError",
    "RiskContribution",
    "load_health_rules",
    "parse_health_rules",
    "score_health",
]

#: Schema version of ``configs/health_rules.yaml``; independent of the feature
#: and scoring versions so the three can move separately.
SCHEMA_VERSION: Final[str] = "1.0"

#: Allowed ``direction`` values of a feature rule.
DIRECTIONS: Final[tuple[str, ...]] = ("increase", "decrease", "two_sided")

#: Tolerance accepted when checking that the weights sum to one.
WEIGHT_SUM_TOLERANCE: Final[float] = 1e-6


class HealthRulesError(ValueError):
    """Raised when a health rule document is rejected.

    Carries the offending field path separately so the CLI can report it
    without parsing the message. It is a :class:`ValueError` for the same reason
    as ``g15.config.ConfigError``.

    Attributes:
        path: Dotted field path, e.g. ``features[2].tolerance``.
        reason: Human readable explanation without the path prefix.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}" if path else reason)


@dataclass(frozen=True)
class FeatureRule:
    """One risk term of the health score.

    Attributes:
        name: Feature name, one of ``g15.analysis.features.FEATURE_NAMES``.
        baseline: Healthy value ``b_i`` of the feature, in the feature's unit.
        tolerance: Increment ``r_i > 0`` that maps to risk ``1.0``; also in the
            feature's unit.
        weight: Weight ``w_i >= 0``; the weights of one rule file sum to 1.
        direction: ``increase``, ``decrease`` or ``two_sided``.
    """

    name: str
    baseline: float
    tolerance: float
    weight: float
    direction: str

    def risk(self, value: float) -> float:
        """Return the clipped risk of one feature value.

        Args:
            value: Observed feature value ``f_i``.

        Returns:
            ``clip((f - b) / r, 0, 1)`` for ``increase``, its mirror for
            ``decrease`` and ``clip(|f - b| / r, 0, 1)`` for ``two_sided``.
        """
        if self.direction == "two_sided":
            deviation = abs(value - self.baseline)
        elif self.direction == "decrease":
            deviation = self.baseline - value
        else:
            deviation = value - self.baseline
        return min(max(deviation / self.tolerance, 0.0), 1.0)


@dataclass(frozen=True)
class HealthRules:
    """A validated, immutable health rule set.

    Attributes:
        schema_version: Always :data:`SCHEMA_VERSION` of this module.
        scoring_version: Version string recorded in every result, so a stored
            score can be traced back to the rules that produced it.
        rule_id: Identifier of the rule set; part of the alarm dedupe key
            ``(device_id, rule_id, start_seq)`` in ``docs/acceptance.md``.
        yellow_threshold: Health score at or below which the window is yellow;
            must be below 100, otherwise a perfect score would be an alarm.
        red_threshold: Health score at or below which the window is red; must be
            lower than ``yellow_threshold`` so a lower score is never a milder
            alarm.
        consecutive_windows: Number of consecutive breaching windows required
            before an alarm is raised.
        recovery_windows: Number of consecutive normal windows required before
            an active alarm is cleared.
        feature_rules: The risk terms, in document order.
    """

    schema_version: str
    scoring_version: str
    rule_id: str
    yellow_threshold: float
    red_threshold: float
    consecutive_windows: int
    recovery_windows: int
    feature_rules: tuple[FeatureRule, ...]

    @property
    def weight_total(self) -> float:
        """Sum of all feature weights (1.0 within tolerance by construction)."""
        return math.fsum(rule.weight for rule in self.feature_rules)


@dataclass(frozen=True)
class RiskContribution:
    """One feature's share of the health score.

    Attributes:
        name: Feature name.
        value: Observed feature value.
        baseline: Configured baseline ``b_i``.
        tolerance: Configured tolerance ``r_i``.
        weight: Configured weight ``w_i``.
        risk: Clipped risk ``risk_i`` in ``[0, 1]``.
    """

    name: str
    value: float
    baseline: float
    tolerance: float
    weight: float
    risk: float

    @property
    def weighted_risk(self) -> float:
        """Return ``w_i * risk_i``, the amount removed from 100."""
        return self.weight * self.risk


@dataclass(frozen=True)
class HealthResult:
    """Health score of one window with its explanation.

    Attributes:
        health_score: Score in ``[0, 100]``; ``100`` is best.
        scoring_version: Rule-set version that produced the score.
        rule_id: Rule-set identifier.
        window_start_seq: Absolute ``seq`` of the window's first sample, or
            ``None`` when the caller did not supply it.
        contributions: One :class:`RiskContribution` per configured feature, in
            document order.
    """

    health_score: float
    scoring_version: str
    rule_id: str
    window_start_seq: int | None
    contributions: tuple[RiskContribution, ...]

    @property
    def drivers(self) -> tuple[str, ...]:
        """Return up to three feature names that pushed the score down most.

        Ties are broken by feature name so the explanation is reproducible.

        Returns:
            Feature names with ``weighted_risk > 0``, strongest first; empty
            when every risk is zero.
        """
        active = [item for item in self.contributions if item.weighted_risk > 0.0]
        active.sort(key=lambda item: (-item.weighted_risk, item.name))
        return tuple(item.name for item in active[:3])

    def explain(self) -> str:
        """Return a one-line, human readable explanation of the score.

        Returns:
            Text naming the score and its main drivers, safe for the SQLite
            ``alarms.reason`` column and for reports.
        """
        if not self.drivers:
            return f"health {self.health_score:.2f}; no feature above baseline"
        parts = []
        for item in self.contributions:
            if item.name in self.drivers:
                parts.append(f"{item.name}={item.value:.4g} (risk {item.risk:.2f})")
        return f"health {self.health_score:.2f}; drivers: " + ", ".join(parts)


def score_health(features: FeatureRecord, rules: HealthRules) -> HealthResult:
    """Score one feature vector against a rule set.

    Args:
        features: Features of the window, from
            :func:`g15.analysis.features.extract_features`.
        rules: Validated rule set.

    Returns:
        The :class:`HealthResult`, clipped to ``[0, 100]``.

    Raises:
        HealthRulesError: if a rule names a feature the record does not carry,
            or if a computed value is not finite. Both mean the score would be
            meaningless, so neither is reported as a healthy ``100``.
    """
    contributions = []
    for rule in rules.feature_rules:
        try:
            value = features.feature_value(rule.name)
        except KeyError as exc:
            raise HealthRulesError(
                f"features.{rule.name}", "feature is missing from the record"
            ) from exc
        if not math.isfinite(value):
            raise HealthRulesError(
                f"features.{rule.name}", f"feature value is not finite: {value!r}"
            )
        contributions.append(
            RiskContribution(
                name=rule.name,
                value=value,
                baseline=rule.baseline,
                tolerance=rule.tolerance,
                weight=rule.weight,
                risk=rule.risk(value),
            )
        )

    total_risk = math.fsum(item.weighted_risk for item in contributions)
    score = min(max(100.0 * (1.0 - total_risk), 0.0), 100.0)
    return HealthResult(
        health_score=score,
        scoring_version=rules.scoring_version,
        rule_id=rules.rule_id,
        window_start_seq=features.window_start_seq,
        contributions=tuple(contributions),
    )


def load_health_rules(path: Path | str) -> HealthRules:
    """Load and validate ``configs/health_rules.yaml``.

    Args:
        path: Path to the rule document.

    Returns:
        The validated, immutable rule set.

    Raises:
        HealthRulesError: if the file is missing, unreadable, not valid YAML or
            does not satisfy the rule schema.
    """
    rules_path = Path(path)
    try:
        text = rules_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HealthRulesError(str(rules_path), "file not found") from exc
    except OSError as exc:
        raise HealthRulesError(str(rules_path), f"cannot read file: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HealthRulesError(str(rules_path), f"invalid YAML: {exc}") from exc
    return parse_health_rules(document)


def parse_health_rules(document: object) -> HealthRules:
    """Validate a parsed rule document.

    Unknown keys are rejected at every level, so a typo such as ``weights``
    instead of ``weight`` cannot silently fall back to a default.

    Args:
        document: Parsed YAML document (mappings and sequences only).

    Returns:
        The validated, immutable rule set.

    Raises:
        HealthRulesError: naming the offending field path.
    """
    root = _mapping(document, "")
    _reject_unknown(
        root,
        {
            "schema_version",
            "scoring_version",
            "rule_id",
            "description",
            "thresholds",
            "features",
        },
        "",
    )

    schema_version = _text(root, "schema_version", "")
    if schema_version != SCHEMA_VERSION:
        raise HealthRulesError(
            "schema_version", f"expected {SCHEMA_VERSION!r}, got {schema_version!r}"
        )
    scoring_version = _text(root, "scoring_version", "")
    rule_id = _text(root, "rule_id", "")
    if "description" in root:
        _text(root, "description", "")

    thresholds = _mapping(_require(root, "thresholds", ""), "thresholds")
    _reject_unknown(
        thresholds,
        {"yellow", "red", "consecutive_windows", "recovery_windows"},
        "thresholds",
    )
    yellow = _score(thresholds, "yellow", "thresholds")
    red = _score(thresholds, "red", "thresholds")
    if yellow >= 100.0:
        raise HealthRulesError(
            "thresholds.yellow",
            f"must be below 100, got {yellow}: a perfect score of 100 would "
            "then be classified yellow and every window would raise an alarm",
        )
    if red >= yellow:
        raise HealthRulesError(
            "thresholds.red",
            f"must be lower than thresholds.yellow ({yellow}), got {red}",
        )
    consecutive = _positive_int(thresholds, "consecutive_windows", "thresholds")
    recovery = _positive_int(thresholds, "recovery_windows", "thresholds")

    return HealthRules(
        schema_version=schema_version,
        scoring_version=scoring_version,
        rule_id=rule_id,
        yellow_threshold=yellow,
        red_threshold=red,
        consecutive_windows=consecutive,
        recovery_windows=recovery,
        feature_rules=_feature_rules(root),
    )


def _feature_rules(root: Mapping[str, object]) -> tuple[FeatureRule, ...]:
    """Parse, validate and cross-check the ``features`` sequence.

    Args:
        root: Document root mapping.

    Returns:
        The feature rules in document order.

    Raises:
        HealthRulesError: on any structural or numeric problem, including
            duplicate names and weights that do not sum to one.
    """
    raw = _require(root, "features", "")
    if not isinstance(raw, list):
        raise HealthRulesError("features", "must be a list")
    if not raw:
        raise HealthRulesError("features", "must contain at least one rule")

    rules: list[FeatureRule] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        path = f"features[{index}]"
        entry = _mapping(item, path)
        _reject_unknown(
            entry, {"name", "baseline", "tolerance", "weight", "direction"}, path
        )
        name = _text(entry, "name", path)
        if name not in FEATURE_NAMES:
            raise HealthRulesError(
                f"{path}.name",
                f"unknown feature {name!r}; expected one of {', '.join(FEATURE_NAMES)}",
            )
        if name in seen:
            raise HealthRulesError(f"{path}.name", f"duplicate feature {name!r}")
        seen.add(name)
        baseline = _finite(entry, "baseline", path)
        tolerance = _finite(entry, "tolerance", path)
        if tolerance <= 0.0:
            raise HealthRulesError(
                f"{path}.tolerance", f"must be positive, got {tolerance}"
            )
        weight = _finite(entry, "weight", path)
        if weight < 0.0:
            raise HealthRulesError(f"{path}.weight", f"must be >= 0, got {weight}")
        direction = _text(entry, "direction", path)
        if direction not in DIRECTIONS:
            raise HealthRulesError(
                f"{path}.direction",
                f"expected one of {', '.join(DIRECTIONS)}, got {direction!r}",
            )
        rules.append(
            FeatureRule(
                name=name,
                baseline=baseline,
                tolerance=tolerance,
                weight=weight,
                direction=direction,
            )
        )

    total = math.fsum(rule.weight for rule in rules)
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise HealthRulesError(
            "features.weight",
            f"weights must sum to 1.0 (+-{WEIGHT_SUM_TOLERANCE}), got {total:.6f}",
        )
    return tuple(rules)


def _require(mapping: Mapping[str, object], key: str, path: str) -> object:
    """Return ``mapping[key]`` or raise a path-aware error.

    Args:
        mapping: Parsed mapping.
        key: Required key.
        path: Parent field path.

    Returns:
        The value.

    Raises:
        HealthRulesError: if the key is absent.
    """
    if key not in mapping:
        raise HealthRulesError(_join(path, key), "is required")
    return mapping[key]


def _reject_unknown(
    mapping: Mapping[str, object], allowed: set[str], path: str
) -> None:
    """Reject keys outside ``allowed``.

    Args:
        mapping: Parsed mapping.
        allowed: Permitted keys.
        path: Parent field path.

    Raises:
        HealthRulesError: naming the first unexpected key.
    """
    for key in mapping:
        if key not in allowed:
            raise HealthRulesError(_join(path, str(key)), "unknown field")


def _mapping(value: object, path: str) -> Mapping[str, object]:
    """Return ``value`` as a mapping or raise.

    Args:
        value: Candidate node.
        path: Field path used in the error.

    Returns:
        The mapping.

    Raises:
        HealthRulesError: if ``value`` is not a mapping.
    """
    if not isinstance(value, dict):
        raise HealthRulesError(path, f"must be a mapping, got {type(value).__name__}")
    return value


def _text(mapping: Mapping[str, object], key: str, path: str) -> str:
    """Return a non-empty string field.

    Args:
        mapping: Parsed mapping.
        key: Field name.
        path: Parent field path.

    Returns:
        The stripped string.

    Raises:
        HealthRulesError: if the value is not a non-empty string.
    """
    value = _require(mapping, key, path)
    if not isinstance(value, str) or not value.strip():
        raise HealthRulesError(_join(path, key), "must be a non-empty string")
    return value.strip()


def _finite(mapping: Mapping[str, object], key: str, path: str) -> float:
    """Return a finite float field.

    Args:
        mapping: Parsed mapping.
        key: Field name.
        path: Parent field path.

    Returns:
        The value as ``float``.

    Raises:
        HealthRulesError: if the value is not a finite number (``bool`` is not a
            number here, because ``weight: true`` would otherwise mean ``1``).
    """
    value = _require(mapping, key, path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HealthRulesError(
            _join(path, key), f"must be a number, got {type(value).__name__}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise HealthRulesError(_join(path, key), "must be finite")
    return number


def _score(mapping: Mapping[str, object], key: str, path: str) -> float:
    """Return a threshold in ``(0, 100]``.

    Args:
        mapping: Parsed mapping.
        key: Field name.
        path: Parent field path.

    Returns:
        The threshold.

    Raises:
        HealthRulesError: if the value is outside ``(0, 100]``.
    """
    value = _finite(mapping, key, path)
    if value <= 0.0 or value > 100.0:
        raise HealthRulesError(_join(path, key), f"must be in (0, 100], got {value}")
    return value


def _positive_int(mapping: Mapping[str, object], key: str, path: str) -> int:
    """Return a positive integer field.

    Args:
        mapping: Parsed mapping.
        key: Field name.
        path: Parent field path.

    Returns:
        The integer.

    Raises:
        HealthRulesError: if the value is not an integer >= 1.
    """
    value = _require(mapping, key, path)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HealthRulesError(
            _join(path, key), f"must be an integer, got {type(value).__name__}"
        )
    if value < 1:
        raise HealthRulesError(_join(path, key), f"must be >= 1, got {value}")
    return value


def _join(prefix: str, name: str) -> str:
    """Return the dotted path ``prefix.name``, tolerating an empty prefix."""
    return f"{prefix}.{name}" if prefix else name
