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


@pytest.mark.parametrize("laboratory_enabled", [False, True])
@pytest.mark.parametrize("duration_seconds", ["15", "0"])
def test_live_cli_serves_real_health_and_stops_without_browser_credentials(
    tmp_path, laboratory_enabled, duration_seconds
):
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
            duration_seconds,
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
            *(
                ["--laboratory-root", str(tmp_path / "laboratory")]
                if laboratory_enabled
                else []
            ),
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
        duration_limit = None if duration_seconds == "0" else float(duration_seconds)
        assert ready["config"]["max_duration_seconds"] == duration_limit
        capture_config = json.loads(
            (Path(ready["session_root"]) / "capture-config.json").read_text(
                encoding="utf-8"
            )
        )
        assert capture_config["config"]["max_duration_seconds"] == duration_limit
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

        def post_live(route, payload, action_header):
            request = Request(
                base + route,
                data=json.dumps(payload).encode(),
                headers={
                    "Origin": base,
                    "Content-Type": "application/json",
                    "X-G15-Console-Action": action_header,
                },
            )
            with opener.open(request, timeout=5) as response:
                assert response.status == 200
                return json.loads(response.read())

        risk = get("/api/live/risk")
        assert risk["session_id"] == status["session_id"]
        assert risk["controls_available"] is True and len(risk["devices"]) == 2
        assert all(
            row["mode"] == "auto" and row["event"] is None for row in risk["devices"]
        )
        selected = catalog["devices"][0]
        identity = {key: selected[key] for key in ("run_id", "device_id")}
        control = {"session_id": status["session_id"], **identity}
        post_live("/api/live/control", {**control, "action": "pause"}, "live-control")
        injection = {
            **control,
            "action": "inject",
            "request_id": "wire-inject-1",
            "fault_type": "step",
            "amplitude": 8,
            "delay_seconds": 0,
            "duration_seconds": 0.2,
        }
        accepted = post_live("/api/live/risk", injection, "live-risk")
        event_id = accepted["request"]["event_id"]
        target = next(
            row for row in accepted["devices"] if row["run_id"] == identity["run_id"]
        )
        assert target["mode"] == "normal" and target["capture_paused"] is True
        assert target["event"]["event_id"] == event_id
        assert target["event"]["status"] == "scheduled"
        assert target["event"]["captured_points"] == 0
        assert (
            accepted["history_count"] == 1 and accepted["request"]["replayed"] is False
        )
        assert all(
            row["mode"] == "auto" and row["event"] is None
            for row in accepted["devices"]
            if row["run_id"] != identity["run_id"]
        )
        replayed = post_live("/api/live/risk", injection, "live-risk")
        assert replayed["request"]["event_id"] == event_id
        assert (
            replayed["request"]["replayed"] is True and replayed["history_count"] == 1
        )
        for changed, expected_code in (
            ({"session_id": "old-session"}, "live_risk_session_changed"),
            ({"amplitude": 9}, "live_risk_request_conflict"),
        ):
            with pytest.raises(HTTPError) as rejected_risk:
                post_live("/api/live/risk", {**injection, **changed}, "live-risk")
            assert rejected_risk.value.code == 409
            assert json.loads(rejected_risk.value.read()) == {"error": expected_code}
        post_live("/api/live/control", {**control, "action": "resume"}, "live-control")
        completed = _wait_for(
            lambda: (
                current
                if (current := get("/api/live/risk"))["history"][0]["status"]
                == "completed"
                else None
            )
        )
        event = completed["history"][0]
        assert event["event_id"] == event_id
        assert (
            event["captured_points"]
            == event["processed_points"]
            == event["duration_points"]
        )
        target = next(
            row for row in completed["devices"] if row["run_id"] == identity["run_id"]
        )
        assert target["mode"] == "normal"
        restored = post_live(
            "/api/live/risk",
            {**control, "action": "automatic", "request_id": "wire-automatic-1"},
            "live-risk",
        )
        assert (
            next(
                row
                for row in restored["devices"]
                if row["run_id"] == identity["run_id"]
            )["mode"]
            == "auto"
        )
        workbench = get("/api/workbench")
        assert workbench["available"] is True and len(workbench["runs"]) == 2
        identities = [row["id"] for row in workbench["runs"]]
        replay = get("/api/workbench/run?id=" + identities[0])
        assert (
            replay["configuration"]["parameters"]["max_duration_seconds"]
            == duration_limit
        )
        assert replay["windows"] and replay["complete"] is True
        assert replay["latency"]["kind"] == "capture_to_commit"
        assert replay["latency"]["mean_seconds"] > 0
        compared = get("/api/workbench/compare?ids=" + ",".join(identities))
        assert len(compared["runs"]) == 2
        assert compared["alignment"] == "elapsed_seconds"
        replay_wave = get("/api/workbench/waveform?id=" + identities[0] + "&count=256")
        assert replay_wave["integrity"]["status"] == "sha256_verified"
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
            waveform = get(
                f'/api/waveform?run_id={row["run_id"]}&device_id={row["device_id"]}&count=256'
            )
            assert waveform["integrity"]["status"] == "sha256_verified"
            assert waveform["count"] == 256
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
        if laboratory_enabled:
            overview = get("/api/laboratory")
            assert overview["available"] is True
            request = Request(
                base + "/api/laboratory/experiments",
                data=json.dumps(overview["presets"][0]).encode(),
                headers={
                    "Origin": base,
                    "Content-Type": "application/json",
                    "X-G15-Console-Action": "laboratory",
                },
            )
            with opener.open(request, timeout=5) as response:
                assert response.status == 202
                identity = json.loads(response.read())["experiment"]["id"]
            detail = _wait_for(
                lambda: (
                    value
                    if (value := get("/api/laboratory/experiment?id=" + identity))[
                        "experiment"
                    ]["status"]
                    in ("completed", "failed")
                    else None
                )
            )
            assert detail["experiment"]["status"] == "completed"
            assert detail["results"]["summary"]["total_points"] == 8192
            assert len(get("/api/catalog")["devices"]) == 2
            experiment_wave = get(
                "/api/laboratory/waveform?id=" + identity + "&count=256"
            )
            assert experiment_wave["count"] == 256
            assert experiment_wave["integrity"]["status"] == "sha256_verified"
            saved = get("/api/workbench/run?id=experiment:" + identity)
            assert saved["configuration"]["parameters"] == overview["presets"][0]
            assert len(saved["windows"]) == 32
            assert saved["latency"]["kind"] == "experiment_elapsed"
        else:
            with pytest.raises(HTTPError) as unavailable:
                opener.open(base + "/api/laboratory", timeout=5)
            assert unavailable.value.code == 503
        stop_file.write_text("stop\n", encoding="utf-8")
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 0, stderr
        startup = json.loads(stdout.splitlines()[0])
        assert startup["duration_limit_seconds"] == duration_limit
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
