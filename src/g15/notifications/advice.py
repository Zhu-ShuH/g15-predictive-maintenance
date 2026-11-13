"""Backend maintenance checks from persisted public alarms; no diagnosis or truth."""

from __future__ import annotations

import math
import re
from typing import Mapping, Sequence

_CHECKS = {
    "mean": "复核振动均值偏移、传感器零点与当前工况。",
    "std": "复核振动波动、运行稳定性与负载变化，并安排复测。",
    "rms": "复核振动 RMS、传感器安装、设备紧固与负载变化。",
    "ptp": "检查峰峰值对应的冲击区段，复核紧固情况和异常工况。",
    "kurtosis_excess": "结合原始波形复核冲击性成分与异常采样点。",
    "dominant_freq_hz": "复核主频变化、转速与采样配置，不直接归因到具体零部件。",
    "crest_factor": "复核峰值因子与冲击区段，检查传感器安装和设备紧固。",
    "zero_crossing_rate": "复核过零率变化、频谱特征与采样质量。",
}
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_MEASUREMENT = re.compile(
    rf"^\s*([a-z][a-z0-9_]*)=({_NUMBER})\s*\(risk\s+({_NUMBER})\)", re.I
)


def maintenance_advice(
    alarms: Sequence[Mapping], states: Sequence[Mapping]
) -> tuple[str, ...]:
    """Map the latest actual alarm at each active rule's level to generic checks.

    This mirrors console state.js's feature mapping. Missing active source records
    yield a visible evidence warning, never an invented component diagnosis.
    """
    levels = {
        row.get("rule_id"): row.get("state", {}).get("active_level")
        for row in states
        if isinstance(row, Mapping)
        and isinstance(row.get("rule_id"), str)
        and isinstance(row.get("state"), Mapping)
    }
    groups: dict[str, list] = {}
    for row in alarms:
        if (
            isinstance(row, Mapping)
            and isinstance(row.get("rule_id"), str)
            and row.get("level") in ("yellow", "red")
            and type(row.get("start_seq")) is int
            and row["start_seq"] >= 0
        ):
            groups.setdefault(row["rule_id"], []).append(row)
    actions = []
    for rule in sorted(set(groups) | set(levels)):
        active = levels.get(rule)
        rows = sorted(
            groups.get(rule, []), key=lambda row: row["start_seq"], reverse=True
        )
        source = (
            next((row for row in rows if row["level"] == active), None)
            if active in ("yellow", "red")
            else next(iter(rows), None)
        )
        if source is None:
            if active in ("yellow", "red"):
                actions.append("活动告警的来源记录缺失，先复核告警证据。")
            continue
        found = []
        match = re.search(
            r"(?:^|;\s*)drivers:\s*([^;]+)", str(source.get("reason", ""))
        )
        if match:
            for part in match[1].split(","):
                value = _MEASUREMENT.match(part)
                if value:
                    feature, number, risk = value.groups()
                    if (
                        math.isfinite(float(number))
                        and math.isfinite(float(risk))
                        and 0 <= float(risk) <= 1
                        and feature.lower() in _CHECKS
                    ):
                        found.append(_CHECKS[feature.lower()])
        actions.extend(
            found
            or ["查看来源告警原因，复核对应特征、采样质量、传感器安装与当前工况。"]
        )
        actions.append(
            "优先安排现场安全核查；是否停机及后续处置由人员按现场规程决定。"
            if active == "red"
            else (
                "安排近期点检并复测相关指标，观察异常是否持续或升级。"
                if active == "yellow"
                else "复核该历史告警的处置与复测记录；如再次出现，按现场流程处理。"
            )
        )
    return tuple(dict.fromkeys(actions))
