"""Application-level tests over real committed SQLite rows."""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path

import pytest

from g15.query.application import (
    AuthenticationError,
    HealthQueryApplication,
    ResourceNotFoundError,
    _window_item,
)
from g15.storage.repository import StorageError
from g15.query.auth import AuthorizationPolicy, TokenVerifier, issue_token
from g15.query.cursor import MAX_CURSOR_BYTES, CursorCodec
from g15.storage.database import connect, initialize_database, transaction

RUN_ID = "run-query-1"
DEVICE_ID = "device-query-1"
ISSUER = "issuer-query-test"
AUDIENCE = "audience-query-test"


def _manifest(run_id: str = RUN_ID, device_id: str = DEVICE_ID) -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "run_id": run_id,
        "scenario_id": "query-scenario",
        "device_id": device_id,
        "seed": 42,
        "sample_rate_hz": 100,
        "start_time_utc": "2026-11-10T00:00:00Z",
        "total_points": 100,
        "units": {"vibration": "g", "temperature": "degC"},
        "status": "complete",
        "parts": [
            {
                "part_id": 0,
                "path": "raw/part-000000.npz",
                "rows": 100,
                "sha256": "0" * 64,
            }
        ],
    }


def _features(start_seq: int) -> str:
    return json.dumps(
        {
            "features_version": "1",
            "features": {},
            "sample_count": 10,
            "sample_rate_hz": 100,
            "window_start_seq": start_seq,
            "scoring_version": "score-v1",
            "rule_id": "health-rule",
            "health_explain": "not projected",
        },
        sort_keys=True,
    )


def _state_with_reset_audit() -> str:
    return json.dumps(
        {
            "state_schema_version": "1",
            "state": {
                "active_level": "yellow",
                "consecutive_breaches": 2,
                "normal_windows": 0,
                "alarm_windows": 2,
                "last_window_start_seq": 40,
                "recovered_at_seq": None,
                "scoring_version": "score-v1",
                "rule_id": "health-rule",
            },
            "reset_event": {
                "reason": "reset-secret-text",
                "previous_state_json": "private-audit-envelope",
            },
            "reset_history": [{"reason": "older-reset-secret"}],
        },
        sort_keys=True,
    )


def _insert_window(connection: sqlite3.Connection, start_seq: int) -> None:
    connection.execute(
        """
        INSERT INTO feature_windows
          (run_id, device_id, start_seq, end_seq_exclusive,
           features_json, health_score)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            RUN_ID,
            DEVICE_ID,
            start_seq,
            start_seq + 10,
            _features(start_seq),
            90.0 - start_seq,
        ),
    )


def _create_run(tmp_path: Path) -> tuple[Path, Path]:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / RUN_ID
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "raw").mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps(_manifest(), sort_keys=True), encoding="utf-8"
    )
    database_path = run_dir / "artifacts" / "results.sqlite"
    connection = connect(database_path)
    initialize_database(connection)
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO runs
              (run_id, scenario_id, schema_version, scoring_version,
               status, started_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                RUN_ID,
                "query-scenario",
                "2.0",
                "score-v1",
                "complete",
                "2026-11-10T00:00:00Z",
            ),
        )
        for start_seq in range(0, 50, 10):
            _insert_window(connection, start_seq)
        for start_seq, rule_id in ((40, "rule-a"), (40, "rule-b"), (20, "rule-a")):
            connection.execute(
                """
                INSERT INTO alarms
                  (run_id, device_id, rule_id, start_seq, level, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    RUN_ID,
                    DEVICE_ID,
                    rule_id,
                    start_seq,
                    "yellow",
                    f"alarm-{rule_id}",
                    "2026-11-10T00:00:00Z",
                ),
            )
        connection.execute(
            """
            INSERT INTO alarm_states
              (run_id, device_id, rule_id, last_end_seq_exclusive, state_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (RUN_ID, DEVICE_ID, "health-rule", 50, _state_with_reset_audit()),
        )
    connection.close()
    return runs_root, database_path


def _policy() -> AuthorizationPolicy:
    return AuthorizationPolicy.from_yaml(f"""
authz_schema_version: "1"
policy_version: "policy-query-test"
subjects:
  subject-a:
    actor_ref: actor-a
    permission_domain: plant-a
    allow:
      - run_id: {RUN_ID}
        device_id: {DEVICE_ID}
  subject-b:
    actor_ref: actor-b
    permission_domain: plant-a
    allow:
      - run_id: {RUN_ID}
        device_id: {DEVICE_ID}
  subject-other:
    actor_ref: actor-other
    permission_domain: plant-b
    allow:
      - run_id: another-run
        device_id: another-device
""")


def _runtime(tmp_path: Path):
    runs_root, database_path = _create_run(tmp_path)
    bearer_key = secrets.token_bytes(32)
    cursor_key = secrets.token_bytes(32)
    events: list[dict[str, object]] = []
    application = HealthQueryApplication(
        runs_root=runs_root,
        token_verifier=TokenVerifier(
            key=bearer_key,
            issuer=ISSUER,
            audience=AUDIENCE,
            clock=lambda: 1_000,
        ),
        authorization_policy=_policy(),
        cursor_codec=CursorCodec(cursor_key),
        event_sink=lambda event: events.append(dict(event)),
    )

    def token(subject: str) -> str:
        return issue_token(
            key=bearer_key,
            issuer=ISSUER,
            audience=AUDIENCE,
            subject=subject,
            now=900,
        )

    return application, token, events, database_path


def test_query_returns_stable_order_and_allowlisted_state(tmp_path: Path) -> None:
    application, token, _events, _database = _runtime(tmp_path)

    response = application.query(
        authorization=f"Bearer {token('subject-a')}",
        run_id=RUN_ID,
        device_id=DEVICE_ID,
        request_id="request-1",
        page_size=2,
    )

    body = response.body
    assert [item["start_seq"] for item in body["windows"]["items"]] == [40, 30]
    assert [
        (item["start_seq"], item["rule_id"]) for item in body["alarms"]["items"]
    ] == [(40, "rule-b"), (40, "rule-a")]
    assert body["versions"] == {
        "manifest_schema_version": "2.0",
        "features_versions": ["1"],
        "state_schema_versions": ["1"],
        "run_scoring_version": "score-v1",
    }
    serialized = json.dumps(body, sort_keys=True)
    for forbidden in (
        "reset_event",
        "reset_history",
        "previous_state_json",
        "reset-secret-text",
        "private_truth",
        "health_explain",
    ):
        assert forbidden not in serialized


def test_pagination_freezes_high_water_across_a_new_commit(tmp_path: Path) -> None:
    application, token, _events, database_path = _runtime(tmp_path)
    authorization = f"Bearer {token('subject-a')}"
    first = application.query(
        authorization=authorization,
        run_id=RUN_ID,
        device_id=DEVICE_ID,
        request_id="request-1",
        page_size=2,
    )
    cursor = first.body["windows"]["next_cursor"]
    alarm_cursor = first.body["alarms"]["next_cursor"]
    assert isinstance(cursor, str)
    assert isinstance(alarm_cursor, str)

    connection = connect(database_path)
    with transaction(connection):
        _insert_window(connection, 50)
        connection.execute(
            """
            INSERT INTO alarms
              (run_id, device_id, rule_id, start_seq, level, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RUN_ID,
                DEVICE_ID,
                "rule-z",
                50,
                "red",
                "newer committed alarm",
                "2026-11-10T00:00:02Z",
            ),
        )
    connection.close()

    second = application.query(
        authorization=authorization,
        run_id=RUN_ID,
        device_id=DEVICE_ID,
        request_id="request-2",
        page_size=2,
        windows_cursor=cursor,
        alarms_cursor=alarm_cursor,
    )

    starts = [item["start_seq"] for item in second.body["windows"]["items"]]
    assert starts == [20, 10]
    assert 50 not in starts
    assert not {40, 30}.intersection(starts)
    alarm_keys = [
        (item["start_seq"], item["rule_id"]) for item in second.body["alarms"]["items"]
    ]
    assert alarm_keys == [(20, "rule-a")]
    assert (50, "rule-z") not in alarm_keys


def test_cursor_cannot_cross_anonymous_actor_scope(tmp_path: Path) -> None:
    application, token, _events, _database = _runtime(tmp_path)
    first = application.query(
        authorization=f"Bearer {token('subject-a')}",
        run_id=RUN_ID,
        device_id=DEVICE_ID,
        request_id="request-1",
        page_size=2,
    )

    with pytest.raises(ResourceNotFoundError):
        application.query(
            authorization=f"Bearer {token('subject-b')}",
            run_id=RUN_ID,
            device_id=DEVICE_ID,
            request_id="request-2",
            page_size=2,
            windows_cursor=first.body["windows"]["next_cursor"],
        )


def test_split_authorization_contexts_cannot_cross_application_instances(
    tmp_path: Path,
) -> None:
    first, token, _events, database_path = _runtime(tmp_path)
    second = HealthQueryApplication(
        runs_root=database_path.parents[2],
        token_verifier=TokenVerifier(
            key=secrets.token_bytes(32),
            issuer=ISSUER,
            audience=AUDIENCE,
            clock=lambda: 1_000,
        ),
        authorization_policy=_policy(),
        cursor_codec=CursorCodec(secrets.token_bytes(32)),
    )
    principal = first.authenticate(
        authorization=f"Bearer {token('subject-a')}",
        request_id="request-authenticate-first",
    )

    with pytest.raises(AuthenticationError):
        second.authorize(
            principal=principal,
            run_id=RUN_ID,
            device_id=DEVICE_ID,
            request_id="request-authorize-second",
        )

    authorized = first.authorize(
        principal=principal,
        run_id=RUN_ID,
        device_id=DEVICE_ID,
        request_id="request-authorize-first",
    )
    with pytest.raises(AuthenticationError):
        second.query_authorized(
            authorized=authorized,
            request_id="request-query-second",
        )


def test_long_alarm_rule_id_uses_a_fixed_size_resolvable_cursor(
    tmp_path: Path,
) -> None:
    application, token, _events, database_path = _runtime(tmp_path)
    long_rule_id = "z-rule-" + ("x" * 8_192)
    connection = connect(database_path)
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO alarms
              (run_id, device_id, rule_id, start_seq, level, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RUN_ID,
                DEVICE_ID,
                long_rule_id,
                40,
                "red",
                "long rule identifier regression",
                "2026-11-10T00:00:03Z",
            ),
        )
    connection.close()
    authorization = f"Bearer {token('subject-a')}"

    first = application.query(
        authorization=authorization,
        run_id=RUN_ID,
        device_id=DEVICE_ID,
        request_id="request-long-rule-first",
        page_size=1,
    )
    cursor = first.body["alarms"]["next_cursor"]
    assert first.body["alarms"]["items"][0]["rule_id"] == long_rule_id
    assert isinstance(cursor, str)
    assert len(cursor.encode("ascii")) <= MAX_CURSOR_BYTES
    assert long_rule_id not in cursor

    second = application.query(
        authorization=authorization,
        run_id=RUN_ID,
        device_id=DEVICE_ID,
        request_id="request-long-rule-second",
        page_size=1,
        alarms_cursor=cursor,
    )
    assert [
        (item["start_seq"], item["rule_id"]) for item in second.body["alarms"]["items"]
    ] == [(40, "rule-b")]


def test_unauthorized_pair_is_hidden_before_filesystem_access(tmp_path: Path) -> None:
    application, token, _events, _database = _runtime(tmp_path)

    with pytest.raises(ResourceNotFoundError):
        application.query(
            authorization=f"Bearer {token('subject-other')}",
            run_id=RUN_ID,
            device_id=DEVICE_ID,
            request_id="request-1",
        )


def test_query_does_not_change_schema_or_business_rows(tmp_path: Path) -> None:
    application, token, _events, database_path = _runtime(tmp_path)

    def snapshot() -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
        connection = sqlite3.connect(database_path)
        schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_schema ORDER BY type, name"
        ).fetchall()
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            for table in (
                "runs",
                "processed_chunks",
                "feature_windows",
                "alarms",
                "alarm_states",
            )
        ]
        connection.close()
        return schema, counts

    before = snapshot()
    application.query(
        authorization=f"Bearer {token('subject-a')}",
        run_id=RUN_ID,
        device_id=DEVICE_ID,
        request_id="request-readonly",
    )
    after = snapshot()

    assert after == before


def test_logs_use_actor_ref_and_never_record_sensitive_inputs(tmp_path: Path) -> None:
    application, token, events, _database = _runtime(tmp_path)
    bearer = token("subject-a")

    application.query(
        authorization=f"Bearer {bearer}",
        run_id=RUN_ID,
        device_id=DEVICE_ID,
        request_id="request-log",
    )

    log_text = json.dumps(events, sort_keys=True)
    assert "actor-a" in log_text
    assert "plant-a" in log_text
    for forbidden in (
        bearer,
        "subject-a",
        RUN_ID,
        DEVICE_ID,
        "reset-secret-text",
        "private_truth",
    ):
        assert forbidden not in log_text


def _window_with_risk_dimensions(
    version: str, dimensions: dict[str, object] | None
) -> tuple[int, int, str, float]:
    payload = json.loads(_features(0))
    payload["features_version"] = version
    if dimensions is not None:
        payload["risk_dimensions"] = dimensions
    return 0, 10, json.dumps(payload), 90.0


def _valid_risk_dimensions() -> dict[str, object]:
    return {
        "level_offset": 0.1,
        "vibration_strength": 0.2,
        "variability": 0.3,
        "impact": 0.4,
        "spectral": None,
    }


def test_window_item_projects_five_dimensions_and_legacy_null() -> None:
    """Version 2 exposes validated groups; version 1 keeps unknown explicit."""
    legacy = _window_item(_window_with_risk_dimensions("1", None))
    assert legacy["risk_dimensions"] is None
    dimensions = _valid_risk_dimensions()
    current = _window_item(_window_with_risk_dimensions("2", dimensions))
    assert current["risk_dimensions"] == dimensions


def test_window_item_rejects_missing_or_misversioned_dimensions() -> None:
    """A version 2 row must carry all five keys; version 1 cannot smuggle them."""
    with pytest.raises(StorageError):
        _window_item(_window_with_risk_dimensions("2", None))
    with pytest.raises(StorageError):
        _window_item(_window_with_risk_dimensions("2", {"impact": 0.2}))
    with pytest.raises(StorageError):
        _window_item(_window_with_risk_dimensions("1", _valid_risk_dimensions()))
    with pytest.raises(StorageError):
        _window_item(_window_with_risk_dimensions("3", _valid_risk_dimensions()))


@pytest.mark.parametrize("invalid", [True, -0.01, 1.01, float("nan"), "0.5"])
def test_window_item_rejects_invalid_dimension_values(invalid: object) -> None:
    """Only finite numbers in [0, 1] or null are public risk dimensions."""
    dimensions = _valid_risk_dimensions()
    dimensions["impact"] = invalid
    with pytest.raises(StorageError):
        _window_item(_window_with_risk_dimensions("2", dimensions))
