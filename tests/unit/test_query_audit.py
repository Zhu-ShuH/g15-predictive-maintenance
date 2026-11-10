"""Unit tests for request tracing (ADR 5.1.7).

The trace carries a request id and a fixed stage sequence. It must never carry
sensitive fields: no token, signature, subject, truth, run/device identity,
cursor or reason. The redaction guarantee is structural (the event model only
holds a stage name and a timestamp), which these tests assert.
"""

from __future__ import annotations

from g15.service.audit import (
    STAGE_AUTHN_CHECKED,
    STAGE_AUTHZ_CHECKED,
    STAGE_RESPONSE_COMPLETED,
    STAGE_SERVER_RECEIVED,
    Trace,
    TraceEvent,
    new_trace,
)


def test_new_trace_starts_at_server_received():
    trace = new_trace()
    assert trace.query_shape == "single_run_single_device_health_v1"
    assert trace.events[0].stage == STAGE_SERVER_RECEIVED
    assert trace.request_id


def test_record_appends_in_order():
    trace = new_trace()
    trace.record(STAGE_AUTHN_CHECKED)
    trace.record(STAGE_AUTHZ_CHECKED)
    trace.record(STAGE_RESPONSE_COMPLETED)
    stages = [e.stage for e in trace.events]
    assert stages == [
        STAGE_SERVER_RECEIVED,
        STAGE_AUTHN_CHECKED,
        STAGE_AUTHZ_CHECKED,
        STAGE_RESPONSE_COMPLETED,
    ]


def test_event_model_holds_only_stage_and_time():
    event = TraceEvent(stage="x", at_epoch_ms=1)
    assert set(vars(event)) == {"stage", "at_epoch_ms"}


def test_trace_dict_has_no_sensitive_keys():
    trace = new_trace()
    payload = trace.as_dict()
    assert set(payload) == {"request_id", "query_shape", "events"}
    for event in payload["events"]:
        assert set(event) == {"stage", "at_epoch_ms"}


def test_request_id_is_unique():
    assert new_trace().request_id != new_trace().request_id
