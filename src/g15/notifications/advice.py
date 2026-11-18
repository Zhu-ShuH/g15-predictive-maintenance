"""Backend maintenance checks from persisted public alarms; no diagnosis or truth."""

from __future__ import annotations

from typing import Mapping, Sequence

from g15.knowledge.catalog import describe_match, match_knowledge


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
        found = [describe_match(item) for item in match_knowledge(source)]
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
