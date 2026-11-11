"""Opt-in D0 measurement using the same validation and evidence path as CLI.

CI skips by default. A target miss is retained as a measured failed gate; this
test asserts harness correctness without asserting a scheduler-dependent peak.
It never authorizes D1/D2 or claims that full Gate 0 has passed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.query_load.evidence import write_evidence  # noqa: E402
from tools.query_load.runner import run_measurement  # noqa: E402


def test_gate0_low_load_query_measurement(tmp_path: Path) -> None:
    if os.environ.get("G15_RUN_QUERY_LOAD") != "1":
        pytest.skip("opt-in: set G15_RUN_QUERY_LOAD=1 to run the query load test")
    concurrent = int(os.environ.get("G15_QUERY_CONCURRENT", "10"))
    report = run_measurement(tmp_path / "runs", concurrent=concurrent)
    output = tmp_path / "evidence"
    write_evidence(output, report)
    print("evidence:", output / "report.json")
    print(
        json.dumps(
            {
                "source": report["source"]["git_sha"],
                "machine": report["source"]["machine"],
                "peaks": report["accounting"]["peaks"],
                "latency_seconds": report["accounting"]["latency_seconds"],
                "paging_counts": report["paging"]["counts"],
                "target_reached": report["target_reached"],
                "exit_code": report["exit_code"],
            },
            sort_keys=True,
        )
    )
    assert report["quality_passed"], report["checks"]
    assert not any(report["checks"].values())
    assert report["gate0_passed"] is False
    assert report["exit_code"] == (0 if report["target_reached"] else 1)
