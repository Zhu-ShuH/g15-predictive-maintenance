"""Opt-in live queries over real sessions, credentials and committed SQLite."""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from g15.live import LiveConfig, LiveRuntime
from g15.live.schema import load_live_session
from g15.query.application import HealthQueryApplication
from g15.query.auth import AuthorizationPolicy, TokenVerifier, issue_token
from g15.query.cursor import CursorCodec
from g15.service.http import create_query_server


def _application(runs_root: Path, run_id: str, device_id: str, **options):
    key = secrets.token_bytes(32)
    pairs = [{"run_id": run_id, "device_id": device_id}]
    subjects = {
        "live-reader": {
            "actor_ref": "actor-live-reader",
            "permission_domain": "live-test-domain",
            "allow": pairs,
        },
        "live-colleague": {
            "actor_ref": "actor-live-colleague",
            "permission_domain": "live-test-domain",
            "allow": pairs,
        },
        "foreign-reader": {
            "actor_ref": "actor-foreign-reader",
            "permission_domain": "foreign-test-domain",
            "allow": [{"run_id": "foreign-run", "device_id": "foreign-device"}],
        },
    }
    policy = AuthorizationPolicy.from_yaml(
        json.dumps(
            {
                "authz_schema_version": "1",
                "policy_version": "live-test-v1",
                "subjects": subjects,
            }
        )
    )
    application = HealthQueryApplication(
        runs_root=runs_root,
        token_verifier=TokenVerifier(
            key=key, issuer="live-test", audience="live-test", clock=lambda: 1000
        ),
        authorization_policy=policy,
        cursor_codec=CursorCodec(secrets.token_bytes(32)),
        event_sink=lambda _event: None,
        **options,
    )
    credentials = {
        subject: "Bearer "
        + issue_token(
            key=key, issuer="live-test", audience="live-test", subject=subject, now=900
        )
        for subject in subjects
    }
    return application, credentials


@contextmanager
def _http_service(application):
    server = create_query_server(application, event_sink=lambda _event: None)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def _request(address, run_id, device_id, authorization=None, **parameters):
    connection = http.client.HTTPConnection(*address, timeout=3)
    target = f"/v1/runs/{run_id}/devices/{device_id}/health"
    if parameters:
        target += "?" + urlencode(parameters)
    headers = {} if authorization is None else {"Authorization": authorization}
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_live_opt_in_requires_an_explicit_boolean(tmp_path, value):
    """Truthy configuration must not silently enable the separate protocol."""
    with pytest.raises(ValueError, match="allow_live_runs must be a bool"):
        _application(tmp_path, "live-run", "live-device", allow_live_runs=value)


RULES_PATH = Path(__file__).resolve().parents[2] / "configs" / "health_rules.yaml"


def _new_runtime(root):
    return LiveRuntime(
        root,
        config=LiveConfig(
            devices=1,
            sample_rate_hz=160,
            window_points=16,
            interval_seconds=0.1,
            max_duration_seconds=10,
            cycle_windows=4,
        ),
        health_rules_path=RULES_PATH,
    )


def _wait_for_windows(run_dir, run_id, device_id, minimum):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        session = load_live_session(run_dir, run_id, device_id)
        assert session["error"] is None, session["error"]
        if session["windows_written"] >= minimum:
            return session
        time.sleep(0.01)
    raise AssertionError(f"real live runtime did not commit {minimum} windows")


@pytest.fixture(scope="module", name="real_live_seed")
def _real_live_seed(tmp_path_factory):
    """Create the source with actual timed capture and drain its sole writer."""
    runtime = _new_runtime(tmp_path_factory.mktemp("live-query-source") / "live")
    runtime.start()
    try:
        pair = runtime.catalog()[0]
        run_dir = runtime.runs_root / pair["run_id"]
        _wait_for_windows(run_dir, pair["run_id"], pair["device_id"], 6)
    finally:
        runtime.stop()
    assert (run_dir / "live-session.json").is_file()
    assert not (run_dir / "manifest.json").exists()
    return runtime.runs_root, pair


@pytest.fixture(name="live_run")
def _live_run(tmp_path, real_live_seed):
    """Isolate each destructive check using a closed real-session copy."""
    source, pair = real_live_seed
    root = shutil.copytree(source, tmp_path / "runs")
    run_dir = root / pair["run_id"]
    return SimpleNamespace(
        root=root,
        run_dir=run_dir,
        run_id=pair["run_id"],
        device_id=pair["device_id"],
        database=run_dir / "artifacts" / "results.sqlite",
    )


def _query_live(run, **options):
    return _application(run.root, run.run_id, run.device_id, **options)


def _assert_hidden(status, body):
    assert status == 404
    assert body["error"] == "not_found"
    assert set(body) == {"error", "request_id", "trace_id"}


@pytest.mark.parametrize("options", [{}, {"allow_live_runs": False}])
def test_actual_live_session_is_hidden_without_explicit_opt_in(live_run, options):
    """The default and explicit false retain the offline-only contract."""
    application, credentials = _query_live(live_run, **options)
    with _http_service(application) as address:
        _assert_hidden(
            *_request(
                address, live_run.run_id, live_run.device_id, credentials["live-reader"]
            )
        )


@pytest.mark.parametrize("authorization", [None, "Bearer invalid-live-token"])
def test_opted_in_live_session_still_requires_real_bearer(live_run, authorization):
    """Opt-in never substitutes for signature verification."""
    application, _credentials = _query_live(live_run, allow_live_runs=True)
    with _http_service(application) as address:
        status, body = _request(
            address, live_run.run_id, live_run.device_id, authorization
        )
    assert status == 401 and body["error"] == "unauthorized"


def test_live_opt_in_preserves_exact_grants_and_safe_projection(live_run):
    """Only the granted resource exposes its committed public query fields."""
    application, credentials = _query_live(live_run, allow_live_runs=True)
    with _http_service(application) as address:
        for run_id, device_id, authorization in (
            (live_run.run_id, live_run.device_id, credentials["foreign-reader"]),
            (live_run.run_id, "ungranted-device", credentials["live-reader"]),
            ("ungranted-run", live_run.device_id, credentials["live-reader"]),
        ):
            _assert_hidden(*_request(address, run_id, device_id, authorization))
        status, body = _request(
            address, live_run.run_id, live_run.device_id, credentials["live-reader"]
        )
    assert status == 200 and len(body["windows"]["items"]) >= 6
    assert body["run_id"] == live_run.run_id
    assert body["device_id"] == live_run.device_id
    assert body["run_status"] == "streaming"
    assert body["versions"]["manifest_schema_version"] == "1"
    assert body["versions"]["features_versions"] == ["1"]
    serialized = json.dumps(body)
    for private in (
        "session_id",
        "private_truth",
        "live-session.json",
        "reset_history",
    ):
        assert private not in serialized
    assert str(live_run.root) not in serialized
    assert credentials["live-reader"] not in serialized


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("schema_version", "2.0"),
        ("status", "complete"),
        ("scenario_id", "offline-scenario"),
        ("started_at", "2020-01-01T00:00:00Z"),
        ("run_id", "another-live-run"),
    ],
)
def test_live_descriptor_and_real_database_identity_conflicts_are_hidden(
    live_run, field, wrong_value
):
    """A real database with conflicting run identity remains hidden."""
    with sqlite3.connect(live_run.database) as connection:
        connection.execute(
            f"UPDATE runs SET {field} = ? WHERE run_id = ?",
            (wrong_value, live_run.run_id),
        )
    application, credentials = _query_live(live_run, allow_live_runs=True)
    with _http_service(application) as address:
        _assert_hidden(
            *_request(
                address, live_run.run_id, live_run.device_id, credentials["live-reader"]
            )
        )


@pytest.mark.parametrize("field", ["run_id", "device_id", "source_kind"])
def test_live_descriptor_cannot_reidentify_or_reclassify_a_session(live_run, field):
    """Descriptor edits cannot replace the granted identity or live source."""
    path = live_run.run_dir / "live-session.json"
    descriptor = json.loads(path.read_text(encoding="utf-8"))
    descriptor[field] = "another-resource"
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    application, credentials = _query_live(live_run, allow_live_runs=True)
    with _http_service(application) as address:
        _assert_hidden(
            *_request(
                address, live_run.run_id, live_run.device_id, credentials["live-reader"]
            )
        )


def test_live_session_with_an_offline_descriptor_is_rejected(live_run):
    """The two publication protocols never act as fallbacks for each other."""
    (live_run.run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    application, credentials = _query_live(live_run, allow_live_runs=True)
    with _http_service(application) as address:
        _assert_hidden(
            *_request(
                address, live_run.run_id, live_run.device_id, credentials["live-reader"]
            )
        )


def _symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create symlinks")
        raise


@pytest.mark.parametrize("part", ["run", "descriptor", "database", "dangling_manifest"])
def test_live_query_rejects_external_paths_and_ambiguous_dangling_manifest(
    live_run, tmp_path, part
):
    """Authorized IDs still cannot escape controlled files or add ambiguity."""
    if part == "run":
        controlled = tmp_path / "linked-runs"
        controlled.mkdir()
        _symlink(controlled / live_run.run_id, live_run.run_dir, directory=True)
        live_run.root = controlled
    elif part == "dangling_manifest":
        _symlink(live_run.run_dir / "manifest.json", tmp_path / "missing-manifest.json")
    else:
        path = (
            live_run.database
            if part == "database"
            else live_run.run_dir / "live-session.json"
        )
        external = tmp_path / (
            "external.sqlite" if part == "database" else "external.json"
        )
        shutil.copy2(path, external)
        path.unlink()
        _symlink(path, external)
    application, credentials = _query_live(live_run, allow_live_runs=True)
    with _http_service(application) as address:
        _assert_hidden(
            *_request(
                address, live_run.run_id, live_run.device_id, credentials["live-reader"]
            )
        )


def test_real_live_appends_keep_old_window_cursor_frozen_and_new_reads_fresh(tmp_path):
    """New commits are visible on refresh and excluded from the old walk."""
    runtime = _new_runtime(tmp_path / "live-appends")
    runtime.start()
    try:
        pair = runtime.catalog()[0]
        run_id, device_id = pair["run_id"], pair["device_id"]
        run_dir = runtime.runs_root / run_id
        _wait_for_windows(run_dir, run_id, device_id, 4)
        application, credentials = _application(
            runtime.runs_root, run_id, device_id, allow_live_runs=True
        )
        with _http_service(application) as address:
            status, first = _request(
                address, run_id, device_id, credentials["live-reader"], page_size=2
            )
            assert status == 200
            cursor = first["windows"]["next_cursor"]
            assert isinstance(cursor, str) and cursor
            original_high = first["windows"]["high_water_start_seq"]
            starts = [item["start_seq"] for item in first["windows"]["items"]]
            _wait_for_windows(run_dir, run_id, device_id, original_high // 16 + 3)
            status, fresh = _request(
                address, run_id, device_id, credentials["live-reader"], page_size=2
            )
            assert status == 200
            assert fresh["windows"]["high_water_start_seq"] > original_high
            assert fresh["windows"]["items"][0]["start_seq"] > original_high
            for authorization, parameter in (
                (credentials["live-colleague"], "windows_cursor"),
                (credentials["live-reader"], "alarms_cursor"),
            ):
                _assert_hidden(
                    *_request(
                        address,
                        run_id,
                        device_id,
                        authorization,
                        page_size=2,
                        **{parameter: cursor},
                    )
                )
            while cursor is not None:
                status, older = _request(
                    address,
                    run_id,
                    device_id,
                    credentials["live-reader"],
                    page_size=2,
                    windows_cursor=cursor,
                )
                assert status == 200
                assert older["windows"]["high_water_start_seq"] == original_high
                starts.extend(item["start_seq"] for item in older["windows"]["items"])
                cursor = older["windows"]["next_cursor"]
            assert starts == list(range(original_high, -1, -16))
            assert len(starts) == len(set(starts))
    finally:
        runtime.stop()
