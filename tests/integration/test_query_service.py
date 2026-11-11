"""Real HTTP -> auth -> application -> connect_readonly -> SQLite integration."""

from __future__ import annotations

import http.client
import json
import secrets
import socket
import sqlite3
import threading
import time
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from g15.query.application import HealthQueryApplication, QueryResponse
from g15.query.auth import AuthorizationPolicy, TokenVerifier, issue_token
from g15.query.cursor import CursorCodec
from g15.service.http import QueryRequestHandler, create_query_server
from g15.storage.database import connect, initialize_database, transaction

RUN_ID = "run-http-1"
DEVICE_ID = "device-http-1"


def _create_real_database(tmp_path: Path) -> tuple[Path, Path]:
    runs_root = tmp_path / "runs"
    run_dir = runs_root / RUN_ID
    (run_dir / "raw").mkdir(parents=True)
    (run_dir / "artifacts").mkdir()
    manifest = {
        "schema_version": "2.0",
        "run_id": RUN_ID,
        "scenario_id": "http-integration",
        "device_id": DEVICE_ID,
        "seed": 7,
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
                "sha256": "1" * 64,
            }
        ],
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
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
                "http-integration",
                "2.0",
                "score-http-1",
                "complete",
                "2026-11-10T00:00:00Z",
            ),
        )
        for start_seq in (0, 10, 20, 30):
            features = json.dumps(
                {
                    "features_version": "1",
                    "features": {},
                    "sample_count": 10,
                    "sample_rate_hz": 100,
                    "window_start_seq": start_seq,
                    "scoring_version": "score-http-1",
                    "rule_id": "health-http",
                    "health_explain": "internal explanation",
                },
                sort_keys=True,
            )
            connection.execute(
                """
                INSERT INTO feature_windows
                  (run_id, device_id, start_seq, end_seq_exclusive,
                   features_json, health_score)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (RUN_ID, DEVICE_ID, start_seq, start_seq + 10, features, 80.0),
            )
        connection.execute(
            """
            INSERT INTO alarms
              (run_id, device_id, rule_id, start_seq, level, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RUN_ID,
                DEVICE_ID,
                "health-http",
                30,
                "yellow",
                "score below threshold",
                "2026-11-10T00:00:01Z",
            ),
        )
        state_json = json.dumps(
            {
                "state_schema_version": "1",
                "state": {
                    "active_level": "yellow",
                    "consecutive_breaches": 1,
                    "normal_windows": 0,
                    "alarm_windows": 1,
                    "last_window_start_seq": 30,
                    "recovered_at_seq": None,
                    "scoring_version": "score-http-1",
                    "rule_id": "health-http",
                },
                "reset_event": {
                    "reason": "operator-private-reset-reason",
                    "previous_state_json": "private-reset-envelope",
                },
            },
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO alarm_states
              (run_id, device_id, rule_id, last_end_seq_exclusive, state_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (RUN_ID, DEVICE_ID, "health-http", 40, state_json),
        )
    connection.close()
    return runs_root, database_path


def _read_json_error(
    url: str, token: str | None = None
) -> tuple[int, dict[str, object]]:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    try:
        with urlopen(Request(url, headers=headers), timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _raw_response(host: str, port: int, request: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=5) as connection:
        connection.settimeout(5)
        connection.sendall(request)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65_536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def test_real_http_authorization_pagination_oracle_and_redaction(
    tmp_path: Path,
) -> None:
    runs_root, database_path = _create_real_database(tmp_path)
    bearer_key = secrets.token_bytes(32)
    cursor_key = secrets.token_bytes(32)
    events: list[dict[str, object]] = []
    policy = AuthorizationPolicy.from_yaml(f"""
authz_schema_version: "1"
policy_version: "policy-http-1"
subjects:
  subject-allowed:
    actor_ref: actor-http-allowed
    permission_domain: plant-http
    allow:
      - run_id: {RUN_ID}
        device_id: {DEVICE_ID}
      - run_id: missing-run
        device_id: {DEVICE_ID}
  subject-other:
    actor_ref: actor-http-other
    permission_domain: plant-other
    allow:
      - run_id: other-run
        device_id: other-device
""")
    application = HealthQueryApplication(
        runs_root=runs_root,
        token_verifier=TokenVerifier(
            key=bearer_key,
            issuer="issuer-http-test",
            audience="audience-http-test",
            clock=lambda: 2_000,
        ),
        authorization_policy=policy,
        cursor_codec=CursorCodec(cursor_key),
        event_sink=lambda event: events.append(dict(event)),
    )
    server = create_query_server(
        application,
        event_sink=lambda event: events.append(dict(event)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}/v1/runs/{RUN_ID}/devices/{DEVICE_ID}/health"

    allowed_token = issue_token(
        key=bearer_key,
        issuer="issuer-http-test",
        audience="audience-http-test",
        subject="subject-allowed",
        now=1_900,
    )
    other_token = issue_token(
        key=bearer_key,
        issuer="issuer-http-test",
        audience="audience-http-test",
        subject="subject-other",
        now=1_900,
    )
    try:
        request = Request(
            f"{base}?page_size=2",
            headers={"Authorization": f"Bearer {allowed_token}"},
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            body = json.loads(response.read())

        oracle_connection = sqlite3.connect(database_path)
        oracle = oracle_connection.execute(
            """
            SELECT start_seq FROM feature_windows
            WHERE run_id = ? AND device_id = ?
            ORDER BY start_seq DESC LIMIT 2
            """,
            (RUN_ID, DEVICE_ID),
        ).fetchall()
        window_plan = oracle_connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT start_seq, end_seq_exclusive, features_json, health_score
            FROM feature_windows
            WHERE run_id = ? AND device_id = ? AND start_seq <= ?
            ORDER BY start_seq DESC LIMIT ?
            """,
            (RUN_ID, DEVICE_ID, 30, 3),
        ).fetchall()
        alarm_plan = oracle_connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT start_seq, rule_id, level, reason, created_at
            FROM alarms
            WHERE run_id = ? AND device_id = ?
              AND (start_seq < ? OR (start_seq = ? AND rule_id <= ?))
            ORDER BY start_seq DESC, rule_id DESC LIMIT ?
            """,
            (RUN_ID, DEVICE_ID, 30, 30, "health-http", 3),
        ).fetchall()
        oracle_connection.close()
        assert any("SEARCH" in str(row[3]).upper() for row in window_plan)
        assert any("SEARCH" in str(row[3]).upper() for row in alarm_plan)
        assert [item["start_seq"] for item in body["windows"]["items"]] == [
            row[0] for row in oracle
        ]
        assert body["windows"]["next_cursor"]
        assert body["versions"]["manifest_schema_version"] == "2.0"

        serialized = json.dumps(body, sort_keys=True)
        for forbidden in (
            "private_truth",
            "reset_event",
            "reset_history",
            "previous_state_json",
            "operator-private-reset-reason",
            "private-reset-envelope",
            "health_explain",
        ):
            assert forbidden not in serialized

        unauth_status, unauth_body = _read_json_error(base)
        invalid_auth_status, invalid_auth_body = _read_json_error(
            base, allowed_token + "x"
        )
        hidden_status, hidden_body = _read_json_error(base, other_token)
        missing_url = (
            f"http://{host}:{port}/v1/runs/missing-run/devices/" f"{DEVICE_ID}/health"
        )
        missing_status, missing_body = _read_json_error(missing_url, allowed_token)
        assert unauth_status == 401
        assert unauth_body["error"] == "unauthorized"
        assert invalid_auth_status == 401
        assert invalid_auth_body["error"] == "unauthorized"
        assert hidden_status == missing_status == 404
        assert hidden_body["error"] == missing_body["error"] == "not_found"
        assert set(hidden_body) == set(missing_body)
        assert not (runs_root / "missing-run").exists()

        invalid_status, invalid_body = _read_json_error(
            f"{base}?windows_cursor=damaged", allowed_token
        )
        assert invalid_status == 400
        assert invalid_body["error"] == "invalid_request"

        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.putrequest("GET", f"/v1/runs/{RUN_ID}/devices/{DEVICE_ID}/health")
        connection.putheader("Authorization", f"Bearer {allowed_token}")
        connection.putheader("Authorization", f"Bearer {allowed_token}")
        connection.endheaders()
        duplicate_response = connection.getresponse()
        duplicate_response.read()
        connection.close()
        assert duplicate_response.status == 401

        # Authentication precedes resource/parameter parsing, and exact
        # authorization precedes optional parameter parsing.
        unauth_bad_path_status, _ = _read_json_error(
            f"http://{host}:{port}/not-a-query?{('x' * 5_000)}"
        )
        invalid_bad_parameter_status, _ = _read_json_error(
            f"{base}?page_size=not-a-number", allowed_token + "x"
        )
        hidden_bad_parameter_status, _ = _read_json_error(
            f"{base}?page_size=not-a-number", other_token
        )
        allowed_bad_parameter_status, allowed_bad_parameter_body = _read_json_error(
            f"{base}?page_size=not-a-number", allowed_token
        )
        assert unauth_bad_path_status == invalid_bad_parameter_status == 401
        assert hidden_bad_parameter_status == 404
        assert allowed_bad_parameter_status == 400
        assert allowed_bad_parameter_body["error"] == "invalid_request"

        # An oversized request line may be rejected by the stdlib transport
        # before application parsing (Windows: 431 with an empty body; some
        # platforms close the connection). Accept either transport rejection
        # or the application-level 400; do not assume a JSON body here.
        overlong_query_value = "9" * 20_000
        try:
            overlong_status, _ = _read_json_error(
                f"{base}?page_size={overlong_query_value}", allowed_token
            )
        except (OSError, json.JSONDecodeError):
            overlong_status = 431
        assert overlong_status in (400, 431)

        for method in ("POST", "OPTIONS", "TRACE", "CONNECT", "BREW"):
            method_connection = http.client.HTTPConnection(host, port, timeout=5)
            method_connection.request(
                method,
                f"/v1/runs/{RUN_ID}/devices/{DEVICE_ID}/health",
            )
            method_response = method_connection.getresponse()
            method_body = json.loads(method_response.read())
            method_connection.close()
            assert method_response.status == 405
            assert method_response.headers["Allow"] == "GET"
            assert method_response.headers["Connection"] == "close"
            assert method_body["error"] == "method_not_allowed"

        path = f"/v1/runs/{RUN_ID}/devices/{DEVICE_ID}/health"
        pipelined = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Authorization: Bearer {allowed_token}\r\n"
            "Content-Length: 32\r\n\r\n"
            "GET /second HTTP/1.1\r\nHost: x\r\n\r\n"
        ).encode("ascii")
        pipelined_response = _raw_response(host, port, pipelined)
        assert pipelined_response.startswith(b"HTTP/1.1 400")
        assert pipelined_response.count(b"HTTP/1.1") == 1
        assert b"Connection: close" in pipelined_response

        transfer_encoded = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Authorization: Bearer {allowed_token}\r\n"
            "Content-Length: 0\r\n"
            "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
        ).encode("ascii")
        assert _raw_response(host, port, transfer_encoded).startswith(b"HTTP/1.1 400")

        duplicate_length = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Authorization: Bearer {allowed_token}\r\n"
            "Content-Length: 0\r\nContent-Length: 0\r\n\r\n"
        ).encode("ascii")
        assert _raw_response(host, port, duplicate_length).startswith(b"HTTP/1.1 400")

        expect_continue = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Authorization: Bearer {allowed_token}\r\n"
            "Content-Length: 1\r\nExpect: 100-continue\r\n\r\n"
        ).encode("ascii")
        expect_response = _raw_response(host, port, expect_continue)
        assert expect_response.startswith(b"HTTP/1.1 400")
        assert b"100 Continue" not in expect_response

        log_text = json.dumps(events, sort_keys=True)
        assert "actor-http-allowed" in log_text
        assert "plant-http" in log_text
        for forbidden in (
            allowed_token,
            other_token,
            "subject-allowed",
            "subject-other",
            RUN_ID,
            DEVICE_ID,
            "operator-private-reset-reason",
            "private_truth",
            "damaged",
        ):
            assert forbidden not in log_text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_server_bounds_workers_and_slow_connections(tmp_path: Path) -> None:
    runs_root, _database_path = _create_real_database(tmp_path)
    bearer_key = secrets.token_bytes(32)
    application = HealthQueryApplication(
        runs_root=runs_root,
        token_verifier=TokenVerifier(
            key=bearer_key,
            issuer="issuer-http-test",
            audience="audience-http-test",
            clock=lambda: 2_000,
        ),
        authorization_policy=AuthorizationPolicy.from_yaml(f"""
authz_schema_version: "1"
policy_version: "policy-http-bounds"
subjects:
  subject-allowed:
    actor_ref: actor-http-bounds
    permission_domain: plant-http
    allow:
      - run_id: {RUN_ID}
        device_id: {DEVICE_ID}
"""),
        cursor_codec=CursorCodec(secrets.token_bytes(32)),
    )
    server = create_query_server(
        application,
        request_timeout_seconds=0.25,
        max_workers=1,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        assert server.max_workers == 1
        assert server.request_timeout_seconds == 0.25

        # Occupying the sole worker permit deterministically exercises the
        # overload path without creating an unbounded thread burst.
        assert server._worker_slots.acquire(
            blocking=False
        )  # pylint: disable=protected-access
        try:
            overload_results = [
                _read_json_error(f"http://{host}:{port}/overloaded-{attempt}")
                for attempt in range(20)
            ]
        finally:
            server._worker_slots.release()  # pylint: disable=protected-access
        assert [status for status, _body in overload_results] == [503] * 20
        assert all(body["error"] == "internal" for _status, body in overload_results)

        with socket.create_connection((host, port), timeout=5) as slow:
            slow.settimeout(0.01)
            started = time.monotonic()
            connection_closed = False
            slow.sendall(b"GET /incomplete HTTP/1.1\r\n")
            for byte in b"X-Slow: incomplete-header":
                try:
                    slow.sendall(bytes([byte]))
                except OSError:
                    connection_closed = True
                    break
                try:
                    connection_closed = slow.recv(1) == b""
                except socket.timeout:
                    pass
                except (ConnectionResetError, ConnectionAbortedError):
                    connection_closed = True
                if connection_closed:
                    break
                time.sleep(0.04)
            if not connection_closed:
                slow.settimeout(1)
                try:
                    connection_closed = slow.recv(1) == b""
                except (ConnectionResetError, ConnectionAbortedError):
                    connection_closed = True
            assert connection_closed
            assert time.monotonic() - started < 0.65

        recovered_status, recovered_body = _read_json_error(
            f"http://{host}:{port}/recovered"
        )
        assert recovered_status == 401
        assert recovered_body["error"] == "unauthorized"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_socket_write_failure_does_not_attempt_a_second_response(monkeypatch) -> None:
    """A failed success write is audited, never followed by a synthetic 500."""

    class FakeApplication:
        @staticmethod
        def authenticate(**_kwargs):
            return object()

        @staticmethod
        def authorize(**_kwargs):
            return object()

        @staticmethod
        def query_authorized(**_kwargs):
            return QueryResponse(
                body={"query_contract_version": "1"},
                actor_ref="actor-write-failure",
                permission_domain="plant-write-failure",
                manifest_schema_version="2.0",
            )

    events: list[dict[str, object]] = []
    handler = object.__new__(QueryRequestHandler)
    handler.close_connection = False
    handler.connection = SimpleNamespace(settimeout=lambda _seconds: None)
    handler.command = "GET"
    handler.path = "/v1/runs/run-1/devices/device-1/health"
    handler.headers = Message()
    handler.headers.add_header("Authorization", "Bearer opaque")
    handler.server = SimpleNamespace(
        application=FakeApplication(),
        event_sink=lambda event: events.append(dict(event)),
        request_timeout_seconds=5.0,
    )
    writes: list[HTTPStatus] = []

    def failed_write(status, _body, **_kwargs):
        writes.append(status)
        return False

    monkeypatch.setattr(handler, "_send_json", failed_write)

    def unexpected_error(*_args, **_kwargs):
        pytest.fail("must not attempt a second response after a write failure")

    monkeypatch.setattr(handler, "_send_error", unexpected_error)
    handler.do_GET()

    assert writes == [HTTPStatus.OK]
    assert [event["result"] for event in events[-2:]] == [
        "connection_error",
        "connection_error",
    ]
