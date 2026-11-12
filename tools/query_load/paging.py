"""Raw and consumed views of a D0 paging walk, with separate verdicts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from .client import QueryClient
from .d0_fixture import (
    D0_RULE_ID,
    D0_WINDOWS_PER_DEVICE,
    device_id,
    device_run_id,
    window_start_seq,
)
from .oracle import verify_response

Response = tuple[int, dict[str, object], str]
MAX_PAGING_PAGES = 20


@dataclass
class PagingWalk:
    """Keep every raw response; consumption never edits the received body."""

    raw: list[Response] = field(default_factory=list)
    consumed: list[Response] = field(default_factory=list)
    discards: list[dict[str, object]] = field(default_factory=list)
    oracle_problems: list[str] = field(default_factory=list)
    traversal_problems: list[str] = field(default_factory=list)

    def evidence(self) -> dict[str, object]:
        def count(results: list[Response], stream: str) -> int:
            return sum(len(_items(body, stream)) for _, body, _ in results)

        return {
            "raw_responses": self.raw,
            "consumed_responses": self.consumed,
            "discards": self.discards,
            "counts": {
                stream: {
                    "raw": count(self.raw, stream),
                    "consumed": count(self.consumed, stream),
                    "discarded": sum(int(row[stream]) for row in self.discards),
                }
                for stream in ("windows", "alarms")
            },
            "raw_oracle_problems": self.oracle_problems,
            "traversal_problems": self.traversal_problems,
            "consumed_coverage_problems": verify_paging_coverage(self.consumed),
            "raw_coverage_observations": verify_paging_coverage(self.raw),
            "raw_duplicate_free_claim": False,
        }


def _items(body: dict[str, object], stream: str) -> list:
    section = body.get(stream)
    items = section.get("items") if isinstance(section, dict) else None
    return items if isinstance(items, list) else []


def run_paging(
    client: QueryClient, device_index: int, db_paths: list[Path]
) -> PagingWalk:
    """Verify raw items before discarding responses of already exhausted streams.

    The protocol cannot signal an exhausted parallel stream. Omitting its
    cursor re-serves page one; this is recorded, never called raw deduplication.
    """
    run_id = device_run_id(device_index)
    device = device_id(device_index)
    db_path = next(path for path in db_paths if path.parent.parent.name == run_id)
    walk = PagingWalk()
    cursors = {"windows": "", "alarms": ""}
    done = {"windows": False, "alarms": False}
    while not all(done.values()):
        if len(walk.raw) >= MAX_PAGING_PAGES:
            walk.traversal_problems.append(
                f"paging exceeded {MAX_PAGING_PAGES} pages: cursor never exhausted"
            )
            break
        status, response = client.query(
            run_id,
            device,
            page_size=10,
            windows_cursor=cursors["windows"],
            alarms_cursor=cursors["alarms"],
        )
        body = deepcopy(response)
        walk.raw.append((status, body, run_id))
        if status == 200:
            walk.oracle_problems.extend(
                f"raw[{len(walk.raw) - 1}] {problem}"
                for problem in verify_response(
                    body,
                    db_path,
                    run_id,
                    device,
                    10,
                    first_page_streams=tuple(
                        stream for stream, cursor in cursors.items() if not cursor
                    ),
                )
            )
        else:
            walk.oracle_problems.append(f"raw[{len(walk.raw) - 1}] status {status}")
        consumed = deepcopy(body)
        discarded: dict[str, object] = {
            "page": len(walk.raw) - 1,
            "request_id": body.get("request_id"),
            "windows": 0,
            "alarms": 0,
            "reason": "stream already exhausted; absent cursor re-serves page one",
        }
        for stream in ("windows", "alarms"):
            section = body.get(stream)
            if not isinstance(section, dict):
                walk.traversal_problems.append(f"missing {stream} page object")
                done[stream] = True
                continue
            if done[stream]:
                discarded[stream] = len(_items(body, stream))
                consumed[stream]["items"] = []
                consumed[stream]["next_cursor"] = None
            else:
                cursor = section.get("next_cursor")
                if cursor is None:
                    done[stream] = True
                    cursors[stream] = ""
                elif not isinstance(cursor, str) or not cursor:
                    walk.traversal_problems.append(f"invalid {stream} cursor")
                    done[stream] = True
                else:
                    cursors[stream] = cursor
        walk.consumed.append((status, consumed, run_id))
        walk.discards.append(discarded)
        if status != 200:
            break
    return walk


def verify_paging_coverage(results: list[Response]) -> list[str]:
    """Check exact D0 keys, multiplicity and descending order of the given view.

    Call on consumed pages for the traversal verdict. Calling on raw pages
    additionally exposes the protocol's re-served exhausted alarm stream.
    """
    problems: list[str] = []
    windows: list[int] = []
    alarms: list[tuple[int, str]] = []
    for position, (status, body, _run) in enumerate(results):
        if status != 200:
            problems.append(f"paging[{position}] status {status} != 200")
            continue
        for stream in ("windows", "alarms"):
            section = body.get(stream)
            if not isinstance(section, dict) or not isinstance(
                section.get("items"), list
            ):
                problems.append(f"paging[{position}] malformed {stream}.items")
                continue
            for item in section["items"]:
                if not isinstance(item, dict) or type(item.get("start_seq")) is not int:
                    problems.append(f"paging[{position}] invalid {stream} key")
                    continue
                if stream == "windows":
                    windows.append(item["start_seq"])
                elif isinstance(item.get("rule_id"), str):
                    alarms.append((item["start_seq"], item["rule_id"]))
                else:
                    problems.append(f"paging[{position}] invalid alarm rule_id")
    expected_windows = {window_start_seq(i) for i in range(D0_WINDOWS_PER_DEVICE)}
    if len(windows) != D0_WINDOWS_PER_DEVICE:
        problems.append(
            f"paging windows total {len(windows)} != {D0_WINDOWS_PER_DEVICE}"
        )
    if set(windows) != expected_windows:
        problems.append(
            f"paging windows set mismatch: missing={sorted(expected_windows - set(windows))} "
            f"extra={sorted(set(windows) - expected_windows)}"
        )
    if windows != sorted(windows, reverse=True):
        problems.append("paging windows out of descending order")
    expected_alarms = {(window_start_seq(D0_WINDOWS_PER_DEVICE - 1), D0_RULE_ID)}
    if len(alarms) != 1:
        problems.append(f"paging alarms total {len(alarms)} != 1")
    if set(alarms) != expected_alarms:
        problems.append("paging alarms set mismatch")
    if alarms != sorted(alarms, reverse=True):
        problems.append("paging alarms out of descending order")
    return problems
