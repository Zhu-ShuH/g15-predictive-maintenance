"""Only explicitly owned, finished simulator sessions enter the replay catalog."""

import json

import pytest

from g15.console.archive import LiveHistoryRegistry


def _archive(root, *, session="live-20260101T000000-0123456789", status="stopped"):
    directory = root / session
    run_id, device = session + "-d01", "LIVE-001"
    run_dir = directory / "runs" / run_id
    run_dir.mkdir(parents=True)
    descriptor = {
        "session_id": session,
        "run_id": run_id,
        "device_id": device,
        "status": status,
        "sample_rate_hz": 256,
        "window_points": 256,
        "started_at_utc": "2026-01-01T00:00:00Z",
    }
    values = {
        directory / "catalog.json": [{"run_id": run_id, "device_id": device}],
        run_dir / "live-session.json": descriptor,
        directory
        / "session-status.json": {
            "session_id": session,
            "status": status,
            "devices": [
                {
                    "run_id": run_id,
                    "device_id": device,
                    "latency": {"count": 2, "mean_seconds": 0.02, "max_seconds": 0.03},
                }
            ],
        },
        directory
        / "capture-config.json": {
            "config": {
                "sample_rate_hz": 256,
                "window_points": 256,
                "interval_seconds": 1,
                "seed": 7,
                "cycle_windows": 12,
                "devices": 1,
            },
            "not_public": "never-return-this",
        },
    }
    for path, value in values.items():
        path.write_text(json.dumps(value), encoding="utf-8")
    return directory, run_dir


def test_finished_sessions_register_exact_resources_and_capture_parameters(tmp_path):
    directory, run_dir = _archive(tmp_path)
    registry = LiveHistoryRegistry(tmp_path, current_session="other")
    row = registry()[0]
    assert row["run_dir"] == run_dir
    assert row["source_kind"] == "archive"
    assert row["session_id"] == directory.name
    assert row["latency"]["mean_seconds"] == 0.02
    assert row["parameters"]["seed"] == 7
    assert "never-return-this" not in str(registry())
    row["parameters"]["seed"] = 999
    assert registry()[0]["parameters"]["seed"] == 7


def test_active_current_and_non_session_directories_are_not_registered(tmp_path):
    directory, _run = _archive(tmp_path, status="running")
    (tmp_path / "not-a-session").mkdir()
    registry = LiveHistoryRegistry(tmp_path, current_session="other")
    assert registry() == [] and registry.skipped_sessions == 1
    assert LiveHistoryRegistry(tmp_path, current_session=directory.name)() == []


@pytest.mark.parametrize(
    "file,changes",
    [
        ("catalog.json", [{"run_id": "../../outside", "device_id": "LIVE-001"}]),
        ("session-status.json", {"session_id": "wrong", "status": "stopped"}),
        ("capture-config.json", {"config": {"seed": "not-a-number"}}),
    ],
)
def test_malformed_archive_is_omitted_without_following_untrusted_identity(
    tmp_path, file, changes
):
    directory, _run = _archive(tmp_path)
    (directory / file).write_text(json.dumps(changes), encoding="utf-8")
    registry = LiveHistoryRegistry(tmp_path, current_session="other")
    assert registry() == [] and registry.skipped_sessions == 1


def test_oversized_catalog_and_linked_ancestors_are_rejected(tmp_path, monkeypatch):
    directory, _run = _archive(tmp_path)
    (directory / "catalog.json").write_bytes(b" " * 131073)
    assert LiveHistoryRegistry(tmp_path, current_session="other")() == []
    from g15.console import archive

    original = archive._regular
    monkeypatch.setattr(
        archive,
        "_regular",
        lambda p, **kw: False if p == directory else original(p, **kw),
    )
    assert LiveHistoryRegistry(tmp_path, current_session="other")() == []


def test_huge_json_integer_does_not_abort_registration_of_other_archives(tmp_path):
    directory, _run = _archive(tmp_path)
    config_path = directory / "capture-config.json"
    value = json.loads(config_path.read_text())
    value["config"]["seed"] = 10**400
    config_path.write_text(json.dumps(value))
    _archive(tmp_path, session="live-20260102T000000-0123456789")
    registry = LiveHistoryRegistry(tmp_path, current_session="other")
    assert registry.skipped_sessions == 1 and len(registry()) == 1
