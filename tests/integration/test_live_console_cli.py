"""A real child-process live service, authenticated upstream and read-only browser API."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest


def _wait_for(predicate, *, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    pytest.fail("live child service did not become ready")


def test_live_cli_serves_real_health_and_stops_without_browser_credentials(tmp_path):
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "runtime"
    stop_file = tmp_path / "stop.request"
    environment = dict(os.environ)
    environment.update(
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PYTHONPATH=str(root / "src") + os.pathsep + str(root),
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "g15.live",
            "--output-root",
            str(output),
            "--health-rules",
            str(root / "configs/health_rules.yaml"),
            "--devices",
            "2",
            "--duration-seconds",
            "15",
            "--sample-rate-hz",
            "2560",
            "--interval-seconds",
            "0.1",
            "--console-port",
            "0",
            "--query-port",
            "0",
            "--stop-file",
            str(stop_file),
        ],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    opener = build_opener(ProxyHandler({}))
    try:
        ready_path = output / "active.json"
        _wait_for(ready_path.is_file)
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        base = ready["console_url"].rstrip("/")

        def get(route):
            with opener.open(base + route, timeout=5) as response:
                assert response.status == 200
                return json.loads(response.read())

        catalog = get("/api/catalog")
        assert catalog["readonly"] is True and len(catalog["devices"]) == 2
        status = _wait_for(
            lambda: (
                (
                    current
                    if all(row["windows_written"] >= 2 for row in current["devices"])
                    else None
                )
                if (current := get("/api/live"))
                else None
            )
        )
        assert status["status"] == "running"
        for row in catalog["devices"]:
            health = get(
                f'/api/health?run_id={row["run_id"]}&device_id={row["device_id"]}'
            )
            assert health["run_status"] == "streaming"
            assert health["windows"]["items"]
            assert health["versions"]["manifest_schema_version"] == "1"
            report = get(
                f'/api/report?run_id={row["run_id"]}&device_id={row["device_id"]}'
            )
            assert report["run_id"] == row["run_id"]
            assert report["device_id"] == row["device_id"]
            assert report["verifiable"] is False and report["passed"] is None
            with pytest.raises(HTTPError) as unauthorized:
                opener.open(
                    ready["query_url"]
                    + f'/v1/runs/{row["run_id"]}/devices/{row["device_id"]}/health',
                    timeout=5,
                )
            assert unauthorized.value.code == 401
        serialized = json.dumps(status) + json.dumps(catalog)
        assert "Bearer" not in serialized and "token" not in serialized.lower()
        with pytest.raises(HTTPError) as rejected:
            opener.open(Request(base + "/api/live", data=b"write"), timeout=5)
        assert rejected.value.code == 405
        stop_file.write_text("stop\n", encoding="utf-8")
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 0, stderr
        final = json.loads(
            (Path(ready["session_root"]) / "service-status.json").read_text(
                encoding="utf-8"
            )
        )
        assert final["status"] == "stopped"
        assert all(
            row["captured_samples"] == row["processed_samples"]
            and row["queue_depth"] == 0
            for row in final["devices"]
        )
        assert "Bearer" not in stdout and "Bearer" not in stderr
    finally:
        if child.poll() is None:
            stop_file.write_text("stop\n", encoding="utf-8")
            try:
                child.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)
