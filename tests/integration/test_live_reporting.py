"""Authorized live report providers and actual concurrent SQLite snapshots."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from g15.console import server as console
from g15.live.reporting import render_live_report
from test_console_service import (  # pylint: disable=unused-import
    _path,
    _request,
    _public_runs,
    _service,
)
from test_live_query import _application, _http_service, _new_runtime, _wait_for_windows


@pytest.mark.parametrize(
    "damage", ["identity_json", "identity_md", "exception", "oversize", "non_bytes"]
)
def test_live_report_provider_rejects_invalid_results_without_private_details(
    service, monkeypatch, damage
):
    """A real successful authorization cannot make unsafe provider output public."""
    calls = []
    secret = "private-provider-secret-/private/session"
    monkeypatch.setattr(console, "MAX_RESPONSE_BYTES", 65536)

    def provider(pair, extension):
        calls.append((pair, extension))
        if damage == "exception":
            raise RuntimeError(secret)
        if damage == "oversize":
            return b"x" * 65537
        if damage == "non_bytes":
            return secret
        if extension == "md":
            return f"| run_id | `other-run` |\\n| device_id | `other-device` |\\n{secret}".encode()
        return json.dumps(
            {"run_id": "other-run", "device_id": "other-device", "private": secret}
        ).encode()

    service.server.application.live_report_provider = provider
    extension = "markdown" if damage == "identity_md" else "json"
    status, _headers, payload = _request(
        service, _path(service, "/api/report/download", format=extension)
    )
    assert status == 502 and json.loads(payload)["error"] == "invalid_report"
    assert len(calls) == 1 and "page_size=1" in service.calls[-1]["target"]
    assert secret.encode() not in payload and service.token.encode() not in payload


def test_failed_upstream_authorization_never_invokes_live_report_provider(service):
    """Provider invocation occurs strictly after actual HTTP signature checks."""
    calls = []
    service.server.application.live_report_provider = lambda *args: calls.append(args)
    service.token_file.write_text("invalid-live-token", encoding="ascii")
    status, _headers, payload = _request(service, _path(service, "/api/report"))
    assert status == 401 and json.loads(payload)["error"] == "unauthorized"
    assert calls == []


@contextmanager
def _running(server):
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_actual_capture_reports_read_database_snapshot_during_report_replacement(
    tmp_path, monkeypatch
):
    """The live path reads no persisted report inode while the writer keeps committing."""
    runtime = _new_runtime(tmp_path / "live-reports")
    runtime.start()
    try:
        pair = runtime.catalog()[0]
        run_id, device_id = pair["run_id"], pair["device_id"]
        run_dir = runtime.runs_root / run_id
        _wait_for_windows(run_dir, run_id, device_id, 2)
        application, credentials = _application(
            runtime.runs_root, run_id, device_id, allow_live_runs=True
        )
        original_open = console.os.open

        def no_persisted_report(path, *args, **kwargs):
            if Path(path).parent.name == "reports" and Path(path).name in {
                "summary.json",
                "summary.md",
            }:
                raise AssertionError("live provider must not open a persisted report")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(console.os, "open", no_persisted_report)
        with _http_service(application) as query_address:
            server = console.create_console_server(
                runtime.catalog_path,
                runtime.runs_root,
                f"http://{query_address[0]}:{query_address[1]}",
                None,
                credential_provider=lambda: credentials["live-reader"][
                    len("Bearer ") :
                ],
                live_report_provider=lambda selected, extension: render_live_report(
                    runtime.runs_root,
                    *selected,
                    extension,
                    live_status=runtime.status_snapshot()["status"],
                ),
            )
            harness = SimpleNamespace(
                server=server, pair={"run_id": run_id, "device_id": device_id}
            )
            with _running(server):
                counts = []
                for index in range(8):
                    status, _headers, payload = _request(
                        harness, _path(harness, "/api/report")
                    )
                    assert status == 200
                    report = json.loads(payload)
                    counts.append(report["feature_windows"])
                    assert report["processed_samples"] == report["feature_windows"] * 16
                    assert (
                        report["health_summary"]["count"] == report["feature_windows"]
                    )
                    assert (
                        0
                        <= report["health_score_min"]
                        <= report["health_score_max"]
                        <= 100
                    )
                    assert report["verifiable"] is False
                    for key in (
                        "passed",
                        "detection_rate",
                        "false_alarm_count",
                        "fault_events",
                        "detected_events",
                        "missed_events",
                        "redundant_alarm_count",
                        "unmatched_alarm_count",
                    ):
                        assert report[key] is None
                    if index == 3:
                        _wait_for_windows(run_dir, run_id, device_id, counts[-1] + 2)
                assert counts == sorted(counts) and counts[-1] > counts[0]
                status, _headers, payload = _request(
                    harness, _path(harness, "/api/report/download", format="markdown")
                )
                assert status == 200
                assert f"| run_id | `{run_id}` |" in payload.decode().splitlines()
                assert f"| device_id | `{device_id}` |" in payload.decode().splitlines()
    finally:
        snapshot = runtime.stop()
    report = json.loads(
        render_live_report(runtime.runs_root, run_id, device_id, "json")
    )
    assert report["feature_windows"] == snapshot["devices"][0]["windows_written"]
    assert report["alarm_count"] == snapshot["devices"][0]["alarms_written"]
    with sqlite3.connect(run_dir / "artifacts" / "results.sqlite") as connection:
        connection.execute(
            "UPDATE runs SET scenario_id='wrong-identity' WHERE run_id=?", (run_id,)
        )
    with pytest.raises(ValueError, match="identity mismatch"):
        render_live_report(runtime.runs_root, run_id, device_id, "json")
