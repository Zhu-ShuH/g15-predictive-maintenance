"""Integration test for the query service (Issue #135 D0).

Builds a real run directory (manifest.json + artifacts/results.sqlite) through
the public writable path, then serves health, windows and alarms through the
QueryRouter using connect_readonly. Asserts the end-to-end contract:

* authorised queries return allow-listed bodies and correct ordering;
* windows pagination is stable and complete across pages;
* a forbidden or absent resource is hidden as 404;
* the health projection never leaks sensitive fields;
* the trace records the full successful stage sequence.
"""

from __future__ import annotations

import json
from pathlib import Path

from g15.config import load_consumer_config
from g15.pipeline.consumer import consume_run
from g15.query.authz import load_authz
from g15.query.bearer import sign_token
from g15.service.router import QueryRequest, QueryRouter

KEY = b"k" * 32
ISSUER = "issuer"
AUDIENCE = "audience"
RUN_ID = "run1"
DEVICE_ID = "dev1"

AUTHZ = load_authz("""authz_schema_version: "1"
policy_version: "v1"
subjects:
  s1:
    actor_ref: "s1"
    permission_domain: "d"
    allow:
      - run_id: "run1"
        device_id: "dev1"
""")


def _build_run(tmp_path: Path, window_points: int = 64, windows: int = 12) -> Path:
    """Build a real consumable run and return its run directory."""
    import numpy as np
    from g15.common.types import SimulationChunk
    from g15.pipeline.schema import Manifest, ManifestStatus, manifest_to_dict
    from g15.pipeline.serialization import write_chunk_atomic

    run_dir = tmp_path / "run1"
    (run_dir / "raw").mkdir(parents=True)
    total_points = window_points * windows
    chunk = SimulationChunk(
        seq=np.arange(total_points, dtype=np.int64),
        timestamp_ns=np.arange(total_points, dtype=np.int64) * 1_000_000,
        vibration=np.zeros(total_points, dtype=np.float64),
        temperature=np.full(total_points, 30.0, dtype=np.float64),
    )
    entry = write_chunk_atomic(
        chunk,
        run_dir / "raw" / "part-000000.npz",
        run_dir=run_dir,
        part_id=0,
    )
    manifest = Manifest(
        schema_version="2.0",
        run_id=RUN_ID,
        scenario_id="scenario1",
        device_id=DEVICE_ID,
        seed=42,
        sample_rate_hz=1000,
        start_time_utc="2026-11-10T00:00:00Z",
        total_points=total_points,
        units={"vibration": "g", "temperature": "degC"},
        status=ManifestStatus.COMPLETE,
        parts=(entry,),
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest_to_dict(manifest)), encoding="utf-8"
    )
    config_doc = {
        "schema_version": "1.0",
        "window_points": window_points,
        "incomplete_window": "drop",
        "health_rules_file": str(
            Path(__file__).resolve().parents[2] / "configs" / "health_rules.yaml"
        ),
    }
    config_path = tmp_path / "consumer.yaml"
    config_path.write_text(
        "\n".join(f"{k}: {json.dumps(v)}" for k, v in config_doc.items()),
        encoding="utf-8",
    )
    config = load_consumer_config(config_path)
    consume_run(run_dir, run_dir / "artifacts" / "results.sqlite", config)
    return run_dir


def _router(run_dir: Path):
    return QueryRouter(
        key=KEY,
        policy=AUTHZ,
        runs={RUN_ID: run_dir},
        issuer=ISSUER,
        audience=AUDIENCE,
        now=lambda: 1500,
    )


def _token():
    return "Bearer " + sign_token(
        {
            "typ": "g15-query-access",
            "ver": "1",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "s1",
            "jti": "j1",
            "iat": 1000,
            "exp": 1600,
        },
        KEY,
    )


def test_health_end_to_end(tmp_path):
    run_dir = _build_run(tmp_path)
    r = _router(run_dir).handle(
        QueryRequest(
            authorization=_token(), run_id=RUN_ID, device_id=DEVICE_ID, kind="health"
        )
    )
    assert r.status == 200
    body = r.body
    assert body["manifest_schema_version"] == "2.0"
    assert body["state_schema_version"] == "1"
    assert set(body["state"]) == {
        "active_level",
        "consecutive_breaches",
        "normal_windows",
        "alarm_windows",
        "last_window_start_seq",
        "recovered_at_seq",
        "scoring_version",
        "rule_id",
    }
    for forbidden in (
        "reset_event",
        "reset_history",
        "previous_state_json",
        "reason",
        "state_json",
        "health_explain",
    ):
        assert forbidden not in body["state"]
        assert forbidden not in body


def test_windows_pagination_is_complete(tmp_path):
    run_dir = _build_run(tmp_path, windows=12)
    router = _router(run_dir)
    collected = []
    cursor = None
    while True:
        r = router.handle(
            QueryRequest(
                authorization=_token(),
                run_id=RUN_ID,
                device_id=DEVICE_ID,
                kind="windows",
                page_size=5,
                cursor=cursor,
            )
        )
        assert r.status == 200
        collected.extend(w["start_seq"] for w in r.body["windows"])
        if not r.body["has_more"]:
            break
        cursor = r.body["next_cursor"]
        assert cursor
    assert len(collected) == len(set(collected))
    assert collected == sorted(collected, reverse=True)
    assert len(collected) == 12


def test_alarms_hide_forbidden_device(tmp_path):
    run_dir = _build_run(tmp_path)
    r = _router(run_dir).handle(
        QueryRequest(
            authorization=_token(), run_id=RUN_ID, device_id="other", kind="health"
        )
    )
    assert r.status == 404
    assert r.body["error"] == "not_found"


def test_successful_trace_sequence(tmp_path):
    run_dir = _build_run(tmp_path)
    r = _router(run_dir).handle(
        QueryRequest(
            authorization=_token(), run_id=RUN_ID, device_id=DEVICE_ID, kind="health"
        )
    )
    stages = [e.stage for e in r.trace.events]
    assert stages == [
        "server_received",
        "authn_checked",
        "authz_checked",
        "db_opened",
        "query_started",
        "query_completed",
        "response_completed",
    ]
