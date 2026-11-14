"""#162 selected streams over real authenticated HTTP and read-only SQLite."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import secrets
import sqlite3
import threading
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from g15.query.application import HealthQueryApplication
from g15.query.auth import AuthorizationPolicy, TokenVerifier, issue_token
from g15.query.cursor import CursorCodec
from g15.service.http import create_query_server
from g15.storage.database import connect, transaction
from tools.query_load.d0_fixture import (
    D0_RULE_ID,
    D0_SCORING_VERSION,
    build_one_device,
    device_id,
    device_run_id,
)

RUN, DEVICE = device_run_id(0), device_id(0)
WINDOW_KEYS = {"sort", "page_size", "high_water_start_seq", "next_cursor", "items"}
ALARM_KEYS = {"sort", "page_size", "high_water", "next_cursor", "items"}
TOP_KEYS = {
    "query_contract_version",
    "run_id",
    "device_id",
    "request_id",
    "trace_id",
    "run_status",
    "versions",
    "committed_high_water",
    "windows",
    "alarms",
    "states",
}


def _snapshot(path):
    with sqlite3.connect(path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return (
            tuple(
                connection.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name"
                )
            ),
            {
                table: tuple(
                    connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
                )
                for table in tables
            },
        )


@contextmanager
def _serve(application):
    server = create_query_server(
        application, max_workers=8, event_sink=lambda _event: None
    )
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def _request(
    service, parameters=(), *, token="reader", run_id=RUN, dev=DEVICE, raw_query=None
):
    query = raw_query if raw_query is not None else urlencode(parameters)
    target = f"/v1/runs/{run_id}/devices/{dev}/health" + ("?" + query if query else "")
    headers = (
        {} if token is None else {"Authorization": service.tokens.get(token, token)}
    )
    connection = http.client.HTTPConnection(*service.address, timeout=4)
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        body = json.loads(payload)
        assert not any(
            secret in payload
            for secret in (
                b"operator-private-reset-reason",
                b"private-reset-envelope",
                service.bearer_key.hex().encode(),
                service.cursor_key.hex().encode(),
            )
        )
        assert not any(
            credential.encode() in payload for credential in service.tokens.values()
        )
        return response.status, body, dict(response.getheaders())
    finally:
        connection.close()


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "runs"
    paths = [build_one_device(root, index) for index in range(2)]
    bearer_key, cursor_key = secrets.token_bytes(32), secrets.token_bytes(32)
    allowed = [
        {"run_id": device_run_id(i), "device_id": device_id(i)} for i in range(2)
    ]
    policy = AuthorizationPolicy.from_yaml(
        json.dumps(
            {
                "authz_schema_version": "1",
                "policy_version": "streams-policy-v1",
                "subjects": {
                    "reader": {
                        "actor_ref": "streams-reader",
                        "permission_domain": "streams-domain",
                        "allow": allowed,
                    },
                    "colleague": {
                        "actor_ref": "streams-colleague",
                        "permission_domain": "other-domain",
                        "allow": allowed,
                    },
                    "outsider": {
                        "actor_ref": "streams-outsider",
                        "permission_domain": "outside-domain",
                        "allow": [
                            {"run_id": "unrelated-run", "device_id": "unrelated-device"}
                        ],
                    },
                },
            }
        )
    )
    application = HealthQueryApplication(
        runs_root=root,
        token_verifier=TokenVerifier(
            key=bearer_key,
            issuer="streams-test",
            audience="streams-test",
            clock=lambda: 1000,
        ),
        authorization_policy=policy,
        cursor_codec=CursorCodec(cursor_key),
        event_sink=lambda _event: None,
    )
    tokens = {
        subject: "Bearer "
        + issue_token(
            key=bearer_key,
            issuer="streams-test",
            audience="streams-test",
            subject=subject,
            now=900,
        )
        for subject in ("reader", "colleague", "outsider")
    }
    with _serve(application) as address:
        yield SimpleNamespace(
            address=address,
            paths=paths,
            tokens=tokens,
            bearer_key=bearer_key,
            cursor_key=cursor_key,
        )


def _assert_error(response, status):
    code, body, headers = response
    assert code == status
    assert set(body) == {"error", "request_id", "trace_id"}
    assert body["request_id"] == body["trace_id"] and body["request_id"]
    if status == 401:
        assert headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.parametrize("streams", [None, "both", "windows", "alarms"])
def test_versioned_shape_selected_data_and_states_are_real_and_readonly(
    service, streams
):
    before = [_snapshot(path) for path in service.paths]
    parameters = [("page_size", "10")] + (
        [] if streams is None else [("streams", streams)]
    )
    status, body, _headers = _request(service, parameters)
    assert status == 200
    assert set(body) == TOP_KEYS
    assert body["query_contract_version"] == ("1" if streams is None else "2")
    assert set(body["windows"]) == WINDOW_KEYS | (
        set() if streams is None else {"omitted"}
    )
    assert set(body["alarms"]) == ALARM_KEYS | (
        set() if streams is None else {"omitted"}
    )
    for name, high_key, top_key, expected_count in (
        ("windows", "high_water_start_seq", "window_start_seq", 10),
        ("alarms", "high_water", "alarm", 1),
    ):
        section = body[name]
        selected = streams in (None, "both", name)
        if streams is not None:
            assert type(section["omitted"]) is bool
            assert section["omitted"] is (not selected)
        assert section["page_size"] == 10
        if selected:
            assert len(section["items"]) == expected_count
            assert section[high_key] is not None
        else:
            assert section["items"] == []
            assert section["next_cursor"] is section[high_key] is None
        assert body["committed_high_water"][top_key] == section[high_key]
    assert body["committed_high_water"]["state_last_end_seq_exclusive"] == 1000
    assert len(body["states"]) == 1
    assert body["states"][0]["state"]["active_level"] == "normal"
    assert body["versions"]["features_versions"] == (
        [] if streams == "alarms" else ["1"]
    )
    if streams != "alarms":
        with sqlite3.connect(service.paths[0]) as connection:
            expected = list(
                connection.execute(
                    "SELECT start_seq,health_score FROM feature_windows ORDER BY start_seq DESC LIMIT 10"
                )
            )
        assert [
            (row["start_seq"], row["health_score"]) for row in body["windows"]["items"]
        ] == expected
    assert [_snapshot(path) for path in service.paths] == before


@pytest.mark.parametrize(
    "query",
    [
        "streams=",
        "streams=WINDOWS",
        "streams=true",
        "streams=1",
        "streams=null",
        "streams=%5B%22windows%22%5D",
        "streams=%7B%7D",
        "streams=windows,alarms",
        "streams=both&streams=both",
        "streams=windows&streams=alarms",
        "streams%5B%5D=windows",
        "streams=windows&extra=1",
    ],
)
def test_http_rejects_invalid_or_duplicate_selection(service, query):
    _assert_error(_request(service, raw_query=query), 400)


@pytest.mark.parametrize(
    "query",
    [
        "streams=bad",
        "streams=both&streams=windows",
        "streams=windows&alarms_cursor=bad",
    ],
)
def test_authentication_and_resource_hiding_precede_optional_selection(service, query):
    _assert_error(_request(service, raw_query=query, token=None), 401)
    _assert_error(_request(service, raw_query=query, token="Bearer invalid"), 401)
    _assert_error(_request(service, raw_query=query, token="outsider"), 404)
    _assert_error(_request(service, raw_query=query, run_id="absent-run"), 404)


@pytest.mark.parametrize(
    "selected,unselected", [("windows", "alarms"), ("alarms", "windows")]
)
@pytest.mark.parametrize("cursor", ["", "malformed", "g15c1.bad.bad"])
def test_omitting_a_stream_cannot_hide_its_cursor(
    service, selected, unselected, cursor
):
    _assert_error(
        _request(service, [("streams", selected), (unselected + "_cursor", cursor)]),
        400,
    )


def _seed_alarm_pages(path):
    connection = connect(path)
    try:
        with transaction(connection):
            for seq in (980, 970):
                connection.execute(
                    "INSERT INTO alarms(run_id,device_id,rule_id,start_seq,level,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        RUN,
                        DEVICE,
                        D0_RULE_ID,
                        seq,
                        "yellow",
                        "public test reason",
                        "2026-11-13T00:00:00Z",
                    ),
                )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "selected,unselected", [("windows", "alarms"), ("alarms", "windows")]
)
def test_even_a_valid_signed_cursor_of_an_omitted_stream_is_rejected(
    service, selected, unselected
):
    _seed_alarm_pages(service.paths[0])
    status, first, _headers = _request(service, [("page_size", 1), ("streams", "both")])
    assert status == 200
    cursor = first[unselected]["next_cursor"]
    assert cursor
    _assert_error(
        _request(
            service,
            [("page_size", 1), ("streams", selected), (unselected + "_cursor", cursor)],
        ),
        400,
    )


def _signed_mutation(cursor, key, **changes):
    prefix, segment, _signature = cursor.split(".")
    payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    payload.update(changes)
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    signing = f"{prefix}.{encoded}"
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(key, signing.encode(), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    return f"{signing}.{signature}"


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor_ref", "other-actor"),
        ("policy_version", "other-policy"),
        ("permission_domain", "other-domain"),
        ("run_id", device_run_id(1)),
        ("device_id", device_id(1)),
        ("query_type", "alarms"),
        ("sort", "other-sort"),
        ("page_size", 2),
    ],
)
def test_selected_cursor_preserves_every_signed_scope_dimension(service, field, value):
    status, first, _headers = _request(
        service, [("page_size", 1), ("streams", "windows")]
    )
    assert status == 200
    cursor = _signed_mutation(
        first["windows"]["next_cursor"], service.cursor_key, **{field: value}
    )
    _assert_error(
        _request(
            service,
            [("page_size", 1), ("streams", "windows"), ("windows_cursor", cursor)],
        ),
        404,
    )


@pytest.mark.parametrize("streams", [None, "both", "windows"])
def test_real_cursor_cannot_cross_subject_or_resource_when_a_stream_is_selected(
    service, streams
):
    status, first, _headers = _request(service, [("page_size", 1)])
    assert status == 200
    parameters = [("page_size", 1), ("windows_cursor", first["windows"]["next_cursor"])]
    if streams is not None:
        parameters.append(("streams", streams))
    _assert_error(_request(service, parameters, token="colleague"), 404)
    _assert_error(
        _request(service, parameters, run_id=device_run_id(1), dev=device_id(1)), 404
    )
    _assert_error(
        _request(
            service,
            [(key, 2 if key == "page_size" else value) for key, value in parameters],
        ),
        404,
    )


@pytest.mark.parametrize(
    "changes", [{"high_water": -1}, {"last_key": -1}, {"last_key": 1000}]
)
def test_valid_signature_does_not_make_invalid_highwater_geometry_valid(
    service, changes
):
    status, first, _headers = _request(
        service, [("page_size", 1), ("streams", "windows")]
    )
    assert status == 200
    cursor = _signed_mutation(
        first["windows"]["next_cursor"], service.cursor_key, **changes
    )
    _assert_error(
        _request(
            service,
            [("page_size", 1), ("streams", "windows"), ("windows_cursor", cursor)],
        ),
        400,
    )


def test_signature_and_query_type_cannot_be_bypassed_with_selection(service):
    _seed_alarm_pages(service.paths[0])
    status, first, _headers = _request(service, [("page_size", 1), ("streams", "both")])
    assert status == 200
    windows = first["windows"]["next_cursor"]
    changed_signature = windows[:-1] + ("A" if windows[-1] != "A" else "B")
    _assert_error(
        _request(
            service,
            [
                ("page_size", 1),
                ("streams", "windows"),
                ("windows_cursor", changed_signature),
            ],
        ),
        400,
    )
    _assert_error(
        _request(
            service,
            [("page_size", 1), ("streams", "alarms"), ("alarms_cursor", windows)],
        ),
        404,
    )
    _assert_error(
        _request(
            service,
            [
                ("page_size", 1),
                ("streams", "windows"),
                ("windows_cursor", first["alarms"]["next_cursor"]),
            ],
        ),
        404,
    )


def test_exhausted_alarm_stream_is_omitted_without_replaying_or_discarding_raw_rows(
    service,
):
    before = _snapshot(service.paths[0])
    windows, alarms, cursor = [], [], None
    responses = []
    for page in range(11):
        parameters = [
            ("page_size", 10),
            ("streams", "both" if page == 0 else "windows"),
        ]
        if cursor:
            parameters.append(("windows_cursor", cursor))
        status, body, _headers = _request(service, parameters)
        assert status == 200
        responses.append(body)
        windows.extend(row["start_seq"] for row in body["windows"]["items"])
        alarms.extend(
            (row["start_seq"], row["rule_id"]) for row in body["alarms"]["items"]
        )
        assert body["alarms"]["omitted"] is (page > 0)
        cursor = body["windows"]["next_cursor"]
        if cursor is None:
            break
    assert len(responses) == 10
    assert windows == list(range(990, -1, -10))
    assert alarms == [(990, D0_RULE_ID)]
    assert _snapshot(service.paths[0]) == before
    # Compatibility is explicit: a fresh v1 request still re-serves page one.
    status, legacy, _headers = _request(service, [("page_size", 10)])
    assert status == 200 and "omitted" not in legacy["alarms"]
    assert legacy["alarms"]["items"][0]["start_seq"] == 990


@pytest.mark.parametrize("continuation", ["both", "windows"])
def test_legacy_signed_window_cursor_can_continue_the_same_scope_in_v2(
    service, continuation
):
    status, legacy, _headers = _request(service, [("page_size", 10)])
    assert status == 200 and legacy["query_contract_version"] == "1"
    status, selected, _headers = _request(
        service,
        [
            ("page_size", 10),
            ("streams", continuation),
            ("windows_cursor", legacy["windows"]["next_cursor"]),
        ],
    )
    assert status == 200 and selected["query_contract_version"] == "2"
    assert selected["windows"]["omitted"] is False
    assert selected["windows"]["high_water_start_seq"] == 990
    assert [row["start_seq"] for row in selected["windows"]["items"]] == list(
        range(890, 790, -10)
    )


@pytest.mark.parametrize(
    "selected,hidden_table", [("alarms", "feature_windows"), ("windows", "alarms")]
)
def test_an_omitted_stream_does_not_read_its_table_but_states_are_still_real(
    service, selected, hidden_table
):
    # Physically rename only the unselected fixture table. A stray high-water
    # or item SELECT against its former name would fail in real SQLite.
    connection = connect(service.paths[0])
    try:
        with transaction(connection):
            connection.execute(
                f'ALTER TABLE "{hidden_table}" RENAME TO "withheld_fixture_rows"'
            )
    finally:
        connection.close()
    before = _snapshot(service.paths[0])
    status, body, _headers = _request(
        service, [("streams", selected), ("page_size", 10)]
    )
    assert status == 200
    omitted = "windows" if selected == "alarms" else "alarms"
    assert body[omitted]["omitted"] is True
    assert body[omitted]["items"] == []
    assert body["states"][0]["state"]["active_level"] == "normal"
    assert body["committed_high_water"]["state_last_end_seq_exclusive"] == 1000
    assert _snapshot(service.paths[0]) == before


def test_selection_keeps_cursor_snapshot_frozen_while_actual_commits_remain_fresh(
    service,
):
    _seed_alarm_pages(service.paths[0])
    status, first, _headers = _request(service, [("page_size", 1), ("streams", "both")])
    assert status == 200
    errors = []

    def append_committed_rows():
        connection = connect(service.paths[0])
        try:
            with transaction(connection):
                envelope = json.loads(
                    connection.execute(
                        "SELECT features_json FROM feature_windows WHERE start_seq=990"
                    ).fetchone()[0]
                )
                envelope["window_start_seq"] = 1000
                connection.execute(
                    "INSERT INTO feature_windows(run_id,device_id,start_seq,end_seq_exclusive,features_json,health_score) VALUES(?,?,?,?,?,?)",
                    (RUN, DEVICE, 1000, 1010, json.dumps(envelope), 77.0),
                )
                connection.execute(
                    "INSERT INTO alarms(run_id,device_id,rule_id,start_seq,level,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        RUN,
                        DEVICE,
                        D0_RULE_ID,
                        1000,
                        "red",
                        "public appended alarm",
                        "2026-11-13T00:00:01Z",
                    ),
                )
                state = json.loads(
                    connection.execute(
                        "SELECT state_json FROM alarm_states"
                    ).fetchone()[0]
                )
                state["state"].update(
                    active_level="red",
                    last_window_start_seq=1000,
                    normal_windows=0,
                    alarm_windows=1,
                )
                connection.execute(
                    "UPDATE alarm_states SET last_end_seq_exclusive=1010,state_json=?",
                    (json.dumps(state),),
                )
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    writer = threading.Thread(target=append_committed_rows)
    writer.start()
    writer.join(timeout=4)
    assert not writer.is_alive() and not errors
    after_commit = _snapshot(service.paths[0])
    windows = [row["start_seq"] for row in first["windows"]["items"]]
    alarms = [row["start_seq"] for row in first["alarms"]["items"]]
    next_cursors = {name: first[name]["next_cursor"] for name in ("windows", "alarms")}
    for _page in range(110):
        active = [name for name, cursor in next_cursors.items() if cursor is not None]
        if not active:
            break
        parameters = [
            ("page_size", 1),
            ("streams", "both" if len(active) == 2 else active[0]),
        ]
        parameters.extend((name + "_cursor", next_cursors[name]) for name in active)
        status, body, _headers = _request(service, parameters)
        assert status == 200
        assert body["committed_high_water"]["state_last_end_seq_exclusive"] == 1010
        assert body["states"][0]["state"]["active_level"] == "red"
        for name, collected in (("windows", windows), ("alarms", alarms)):
            section = body[name]
            collected.extend(row["start_seq"] for row in section["items"])
            next_cursors[name] = section["next_cursor"]
            assert section["omitted"] is (name not in active)
            if name in active:
                high = (
                    section["high_water_start_seq"]
                    if name == "windows"
                    else section["high_water"]["start_seq"]
                )
                assert high == 990
    assert all(cursor is None for cursor in next_cursors.values())
    assert windows == list(range(990, -1, -10))
    assert alarms == [990, 980, 970]
    status, fresh, _headers = _request(service, [("page_size", 1), ("streams", "both")])
    assert status == 200
    assert (
        fresh["windows"]["items"][0]["start_seq"]
        == fresh["alarms"]["items"][0]["start_seq"]
        == 1000
    )
    assert _snapshot(service.paths[0]) == after_commit
