"""Real HTTP tool checks bind omission/version to caller intent, not the body."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from g15.query.auth import issue_token  # noqa: E402
from tools.query_load.accounting import RunAccounting  # noqa: E402
from tools.query_load.client import QueryClient  # noqa: E402
from tools.query_load.d0_fixture import (  # noqa: E402
    D0_ACTOR_REF,
    D0_SUBJECT,
    build_d0_runs,
    device_id,
    device_run_id,
)
from tools.query_load.driver import (  # noqa: E402
    BEARER_AUDIENCE,
    BEARER_ISSUER,
    build_application,
    start_server,
    verify_all,
)
from tools.query_load.oracle import verify_response  # noqa: E402
from tools.query_load.paging import run_paging, verify_paging_coverage  # noqa: E402


@pytest.fixture(scope="module")
def real_streams(tmp_path_factory):
    root = tmp_path_factory.mktemp("tool-stream-contract")
    paths = build_d0_runs(root / "runs")
    bearer_key, cursor_key = b"b" * 32, b"c" * 32
    app, _events = build_application(root / "runs", bearer_key, cursor_key)
    accounting = RunAccounting()
    server, thread, url = start_server(app, accounting)
    token = issue_token(
        key=bearer_key,
        issuer=BEARER_ISSUER,
        audience=BEARER_AUDIENCE,
        subject=D0_SUBJECT,
        now=1900,
    )
    client = QueryClient(url, token, D0_ACTOR_REF, accounting)
    try:
        bodies = {}
        for streams in (None, "both", "windows", "alarms"):
            status, body = client.query(
                device_run_id(0), device_id(0), page_size=10, streams=streams
            )
            assert status == 200
            bodies[streams] = body
        walk = run_paging(client, 0, paths)
        legacy = run_paging(client, 0, paths, legacy=True)
        yield paths, bodies, walk, legacy
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("streams", [None, "both", "windows", "alarms"])
def test_oracle_accepts_each_actual_request_projection(real_streams, streams):
    paths, bodies, _walk, _legacy = real_streams
    assert (
        verify_response(
            bodies[streams],
            paths[0],
            device_run_id(0),
            device_id(0),
            10,
            first_page=True,
            streams=streams,
        )
        == []
    )


def test_selected_paging_retains_request_context_and_raw_coverage(real_streams):
    paths, _bodies, walk, _legacy = real_streams
    assert [row["streams"] for row in walk.request_contexts] == ["both"] + [
        "windows"
    ] * 9
    assert walk.request_contexts[0]["first_page_streams"] == ["windows", "alarms"]
    assert all(row["first_page_streams"] == [] for row in walk.request_contexts[1:])
    assert all(
        set(row) == {"request_id", "streams", "page_size", "first_page_streams"}
        for row in walk.request_contexts
    )
    assert walk.evidence()["counts"] == {
        "windows": {"raw": 100, "consumed": 100, "discarded": 0},
        "alarms": {"raw": 1, "consumed": 1, "discarded": 0},
    }
    assert verify_all(walk.raw, paths, request_contexts=walk.request_contexts) == []
    assert walk.oracle_problems == []
    assert verify_paging_coverage(walk.raw) == []
    assert verify_paging_coverage(walk.consumed) == []
    assert walk.evidence()["raw_duplicate_free_claim"] is True


def test_explicit_legacy_walk_keeps_replay_as_failed_raw_coverage(real_streams):
    paths, _bodies, _walk, legacy = real_streams
    assert all(row["streams"] is None for row in legacy.request_contexts)
    assert verify_all(legacy.raw, paths, request_contexts=legacy.request_contexts) == []
    assert legacy.oracle_problems == []
    assert verify_paging_coverage(legacy.consumed) == []
    assert verify_paging_coverage(legacy.raw) == ["paging alarms total 10 != 1"]
    assert legacy.evidence()["counts"]["alarms"] == {
        "raw": 10,
        "consumed": 1,
        "discarded": 9,
    }
    assert legacy.evidence()["raw_duplicate_free_claim"] is False


@pytest.mark.parametrize(
    "damage",
    [
        "fake_omitted_selected",
        "fake_selected_omitted",
        "replay_omitted_alarm",
        "downgrade",
        "omitted_missing",
        "omitted_not_bool",
        "unknown_omitted_field",
        "omitted_cursor",
        "omitted_highwater",
        "top_omitted_highwater",
        "selected_highwater",
        "hide_states",
        "hide_feature_versions",
    ],
)
def test_oracle_rejects_forged_projection_even_when_response_claims_omitted(
    real_streams, damage
):
    paths, bodies, walk, _legacy = real_streams
    first = damage == "fake_omitted_selected"
    page = deepcopy(walk.raw[0 if first else 1])
    context = deepcopy(walk.request_contexts[0 if first else 1])
    body = page[1]
    if damage == "fake_omitted_selected":
        body["windows"].update(
            omitted=True, items=[], next_cursor=None, high_water_start_seq=None
        )
        body["committed_high_water"]["window_start_seq"] = None
        body["versions"]["features_versions"] = []
    elif damage == "fake_selected_omitted":
        body["alarms"]["omitted"] = False
    elif damage == "replay_omitted_alarm":
        body["alarms"]["items"] = deepcopy(bodies["both"]["alarms"]["items"])
    elif damage == "downgrade":
        body["query_contract_version"] = "1"
        for stream in ("windows", "alarms"):
            del body[stream]["omitted"]
    elif damage == "omitted_missing":
        del body["alarms"]["omitted"]
    elif damage == "omitted_not_bool":
        body["alarms"]["omitted"] = 1
    elif damage == "unknown_omitted_field":
        body["alarms"]["private_reset_audit"] = {"reason": "private"}
    elif damage == "omitted_cursor":
        body["alarms"]["next_cursor"] = "fabricated"
    elif damage == "omitted_highwater":
        body["alarms"]["high_water"] = deepcopy(bodies["both"]["alarms"]["high_water"])
    elif damage == "top_omitted_highwater":
        body["committed_high_water"]["alarm"] = deepcopy(
            bodies["both"]["committed_high_water"]["alarm"]
        )
    elif damage == "selected_highwater":
        body["windows"]["high_water_start_seq"] = 0
    elif damage == "hide_states":
        body["states"] = []
    else:
        body["versions"]["features_versions"] = []
    assert verify_all([page], paths, request_contexts=[context])


@pytest.mark.parametrize(
    "damage",
    ["missing_context", "wrong_stream_context", "wrong_request_id", "wrong_page_size"],
)
def test_revalidation_requires_original_request_context(real_streams, damage):
    paths, _bodies, walk, _legacy = real_streams
    contexts = deepcopy(walk.request_contexts)
    if damage == "missing_context":
        contexts.clear()
    elif damage == "wrong_stream_context":
        contexts[1]["streams"] = "both"
    elif damage == "wrong_request_id":
        contexts[1]["request_id"] = "unrelated-request"
    else:
        contexts[1]["page_size"] = True
    assert verify_all(walk.raw, paths, request_contexts=contexts)


def test_legacy_request_cannot_accept_v2_body_by_response_self_selection(real_streams):
    paths, bodies, _walk, _legacy = real_streams
    assert verify_response(
        bodies["windows"],
        paths[0],
        device_run_id(0),
        device_id(0),
        10,
        first_page=True,
        streams=None,
    )
