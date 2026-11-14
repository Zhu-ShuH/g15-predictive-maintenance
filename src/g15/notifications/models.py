"""Strict policy and public snapshots for mobile alert delivery."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import re
from typing import Mapping, Sequence

MAX_CONTENT_BYTES = 128 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


@dataclass(frozen=True)
class NotificationPolicy:
    """Per-device run red-event threshold and bounded delivery retry policy."""

    enabled: bool = False
    red_threshold: int = 1
    cooldown_seconds: int = 600
    max_attempts: int = 3
    retry_seconds: int = 30

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("invalid notification enabled flag")
        for name, lower, upper in (
            ("red_threshold", 1, 1000000),
            ("cooldown_seconds", 0, 86400),
            ("max_attempts", 1, 5),
            ("retry_seconds", 1, 3600),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("invalid notification policy value")

    def public(self) -> dict:
        """Return only non-secret configuration fields."""
        return asdict(self)


@dataclass(frozen=True)
class AlertSnapshot:
    """One complete exact run/device committed snapshot, with full public report."""

    run_id: str
    device_id: str
    red_count: int
    last_red_id: str | None
    report_json: Mapping
    report_markdown: str
    advice: Sequence[str]
    complete: bool = True

    def __post_init__(self) -> None:
        for value in (self.run_id, self.device_id):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError("invalid notification resource identity")
        if type(self.red_count) is not int or not 0 <= self.red_count <= 1000000000:
            raise ValueError("invalid red event count")
        if self.red_count == 0:
            if self.last_red_id is not None:
                raise ValueError("unexpected red event identity")
        elif not isinstance(self.last_red_id, str) or not _ID.fullmatch(
            self.last_red_id
        ):
            raise ValueError("missing red event identity")
        if type(self.complete) is not bool:
            raise ValueError("invalid snapshot completeness")
        if not isinstance(self.report_json, Mapping):
            raise ValueError("invalid notification report")
        if (
            self.report_json.get("run_id") != self.run_id
            or self.report_json.get("device_id") != self.device_id
        ):
            raise ValueError("notification report resource mismatch")
        if (
            not isinstance(self.report_markdown, str)
            or not self.report_markdown.strip()
        ):
            raise ValueError("missing full markdown report")
        if isinstance(self.advice, (str, bytes)) or not isinstance(
            self.advice, Sequence
        ):
            raise ValueError("invalid maintenance advice")
        if len(self.advice) > 64 or any(
            not isinstance(value, str) or not value.strip() or len(value) > 2000
            for value in self.advice
        ):
            raise ValueError("invalid maintenance advice")
        try:
            json.dumps(dict(self.report_json), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("invalid public JSON report") from None

    def content(self) -> str:
        """Build the full immutable delivery body; never silently truncate reports."""
        advice = self.advice or ("复核来源告警、采样质量和当前工况；处置需人工确认。",)
        # JSON is inside a four-backtick fence so ordinary Markdown reports remain intact.
        body = (
            "# 红色告警维护通知\n\n"
            f"设备：{self.device_id}\n\n运行：{self.run_id}\n\n"
            f"本运行累计红色告警事件：{self.red_count}\n\n"
            "## 维护建议\n\n"
            + "\n".join(f"- {item}" for item in advice)
            + "\n\n所有建议需人工确认，通知不执行设备操作。\n\n"
            "## 完整 Markdown 报告\n\n"
            + self.report_markdown
            + "\n\n## 完整 JSON 报告\n\n````json\n"
            + json.dumps(
                dict(self.report_json),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n````\n"
        )
        if len(body.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError("notification_content_too_large")
        return body


def require_time(value: float) -> float:
    """Validate a finite UTC epoch supplied by a clock."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("invalid notification clock")
    return float(value)
