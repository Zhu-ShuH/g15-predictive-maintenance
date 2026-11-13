"""Meaningful notification boundaries with fake providers and real durable SQLite."""

from dataclasses import replace
import json
import sqlite3
import threading

import pytest

from g15.notifications import (
    AlertSnapshot,
    NotificationPolicy,
    NotificationService,
    NotificationStorageError,
    ProviderError,
    ProviderReceipt,
    compose_message,
    maintenance_advice,
)

TOKEN = "fake-test-token-12345678"


class FakeProvider:
    def __init__(self, failures=()):
        self.calls = []
        self.failures = list(failures)

    def send(self, token, title, content):
        self.calls.append((token, title, content))
        if self.failures:
            raise self.failures.pop(0)
        return ProviderReceipt("test-reference")


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def snapshot(count=1, *, device="device-1", run="run-1", **changes):
    report = {
        "run_id": run,
        "device_id": device,
        "verifiable": False,
        "passed": None,
        "detection_rate": None,
        "false_alarm_count": None,
    }
    values = {
        "run_id": run,
        "device_id": device,
        "red_count": count,
        "last_red_id": f"rule:{count * 256}" if count else None,
        "report_json": report,
        "report_markdown": f"# Full report\n\nrun={run}; device={device}\n\nverifiable=false; passed=null.",
        "advice": ("复核 RMS 与传感器安装。",),
    }
    values.update(changes)
    return AlertSnapshot(**values)


def service(tmp_path, *, policy=None, provider=None, clock=None):
    engine = NotificationService(
        tmp_path / "outbox.sqlite",
        policy or NotificationPolicy(enabled=True),
        transport=provider or FakeProvider(),
        clock=clock or Clock(),
    )
    engine.configure(engine.policy, TOKEN)
    return engine


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"red_threshold": True},
        {"red_threshold": 0},
        {"red_threshold": 1000001},
        {"red_threshold": 1.5},
        {"cooldown_seconds": -1},
        {"cooldown_seconds": 86401},
        {"cooldown_seconds": float("nan")},
        {"max_attempts": 0},
        {"max_attempts": 6},
        {"retry_seconds": 0},
    ],
)
def test_policy_rejects_invalid_values(changes):
    with pytest.raises(ValueError):
        NotificationPolicy(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"red_count": True},
        {"red_count": -1},
        {"last_red_id": None},
        {"last_red_id": "../x"},
        {"device_id": "../device"},
        {"complete": 1},
        {"report_markdown": ""},
        {"advice": "text instead of sequence"},
        {"report_json": {"run_id": "other", "device_id": "device-1"}},
    ],
)
def test_snapshot_rejects_ambiguous_or_wrong_resource_data(changes):
    with pytest.raises(ValueError):
        snapshot(**changes)


def test_threshold_is_inclusive_but_disabled_and_partial_do_not_queue(tmp_path):
    engine = service(tmp_path, policy=NotificationPolicy(red_threshold=2))
    assert engine.observe(snapshot(2))["decision"] == "disabled"
    engine.configure(replace(engine.policy, enabled=True))
    assert engine.observe(snapshot(1))["decision"] == "below_threshold"
    assert (
        engine.observe(snapshot(2, complete=False))["decision"] == "incomplete_snapshot"
    )
    assert engine.status()["counts"] == {}
    assert engine.observe(snapshot(2))["decision"] == "queued"
    assert engine.observe(snapshot(2))["decision"] == "already_queued"
    assert engine.status()["counts"] == {"queued": 1}


def test_missing_token_cannot_send_or_consume_red_event(tmp_path):
    engine = service(tmp_path)
    engine.configure(engine.policy, "")
    assert engine.observe(snapshot())["decision"] == "token_missing"
    assert engine.enqueue_test(snapshot())["decision"] == "token_missing"
    engine.configure(engine.policy, TOKEN)
    assert engine.observe(snapshot())["decision"] == "queued"


def test_cooldown_aggregates_new_red_events_and_does_not_resend_same_event(tmp_path):
    clock = Clock()
    engine = service(tmp_path, clock=clock)
    first = engine.observe(snapshot(1))
    assert first["decision"] == "queued"
    clock.value += 5
    pending = engine.observe(snapshot(2))
    assert pending["decision"] == "queued" and pending["eligible_at"] == 1600
    merged = engine.observe(snapshot(3))
    assert merged["decision"] == "coalesced" and merged["id"] == pending["id"]
    clock.value = 1600
    assert engine.observe(snapshot(3))["decision"] == "already_queued"
    clock.value += 600
    assert engine.observe(snapshot(3))["decision"] == "already_queued"
    assert engine.status()["counts"] == {"queued": 2}
    history = engine.status()["latest"]
    assert {item["red_count"] for item in history} == {1, 3}
    assert (
        next(item for item in history if item["id"] == pending["id"])["next_attempt_at"]
        == 1600
    )


def test_cursors_are_exact_run_and_device_scoped(tmp_path):
    engine = service(tmp_path)
    for row in [snapshot(), snapshot(device="device-2"), snapshot(run="run-2")]:
        assert engine.observe(row)["decision"] == "queued"
    assert engine.status()["counts"] == {"queued": 3}
    assert (
        engine.observe(snapshot(2, last_red_id="rule:256"))["decision"]
        == "already_queued"
    )


def test_stale_and_inconsistent_snapshots_never_advance_or_duplicate_cursor(tmp_path):
    engine = service(tmp_path)
    engine.observe(snapshot(3))
    assert engine.observe(snapshot(2))["decision"] == "stale_snapshot"
    assert (
        engine.observe(snapshot(3, last_red_id="other:10"))["decision"]
        == "inconsistent_snapshot"
    )
    assert engine.observe(snapshot(3))["decision"] == "already_queued"
    assert engine.status()["counts"] == {"queued": 1}


def test_enqueue_rollback_preserves_eligibility(tmp_path):
    engine = service(tmp_path)
    path = tmp_path / "outbox.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER deny_outbox BEFORE INSERT ON notification_outbox BEGIN SELECT RAISE(ABORT,'private-path-token'); END"
        )
    with pytest.raises(NotificationStorageError, match="^notification_storage_error$"):
        engine.observe(snapshot())
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM notification_sources").fetchone()[
                0
            ]
            == 0
        )
        connection.execute("DROP TRIGGER deny_outbox")
    assert engine.observe(snapshot())["decision"] == "queued"


def test_outbox_and_policy_survive_restart_but_token_does_not(tmp_path):
    first = service(
        tmp_path,
        policy=NotificationPolicy(enabled=True, red_threshold=2, cooldown_seconds=99),
    )
    first.observe(snapshot(2))
    first.close()
    second = NotificationService(
        tmp_path / "outbox.sqlite", transport=FakeProvider(), clock=Clock()
    )
    assert second.policy == first.policy
    assert second.status()["configured"] is False
    assert second.dispatch_once()["decision"] == "token_missing"
    second.configure(second.policy, TOKEN)
    assert second.observe(snapshot(2))["decision"] == "already_queued"
    assert second.dispatch_once()["decision"] == "provider_accepted"
    assert second.status()["latest"][0]["provider_reference"] == "test-reference"
    assert second.status()["delivery_semantics"] == "provider_acceptance_only"
    with sqlite3.connect(tmp_path / "outbox.sqlite") as connection:
        dump = "\n".join(connection.iterdump())
    assert TOKEN not in dump
    assert TOKEN not in json.dumps(second.status())


def test_explicit_test_is_allowed_while_disabled_and_does_not_consume_auto_cursor(
    tmp_path,
):
    engine = service(tmp_path, policy=NotificationPolicy())
    assert engine.enqueue_test(snapshot())["decision"] == "queued"
    assert engine.enqueue_test(snapshot())["decision"] == "queued"
    assert engine.dispatch_once()["decision"] == "provider_accepted"
    engine.configure(replace(engine.policy, enabled=True))
    assert engine.observe(snapshot())["decision"] == "queued"
    assert engine.status()["counts"] == {"provider_accepted": 1, "queued": 2}


def test_disabling_pauses_pending_automatic_delivery_but_keeps_evidence(tmp_path):
    provider = FakeProvider()
    engine = service(tmp_path, provider=provider)
    engine.observe(snapshot())
    engine.configure(replace(engine.policy, enabled=False))
    assert engine.dispatch_once()["decision"] == "idle"
    assert not provider.calls
    assert engine.status()["counts"] == {"queued": 1}


def test_full_preview_exactly_matches_sent_content_and_preserves_null_verdict(tmp_path):
    provider = FakeProvider()
    engine = service(tmp_path, provider=provider)
    row = snapshot()
    expected = compose_message(row, 1)
    engine.observe(row)
    assert engine.dispatch_once()["decision"] == "provider_accepted"
    _, title, markdown = provider.calls[0]
    assert title == expected["title"]
    assert markdown == expected["markdown"]
    assert row.report_markdown in markdown
    assert '"verifiable": false' in markdown
    assert '"detection_rate": null' in markdown
    assert row.advice[0] in markdown


def test_oversized_complete_report_fails_explicitly_without_cursor_or_truncation(
    tmp_path,
):
    engine = service(tmp_path)
    row = snapshot(report_markdown="FULL-START" + "x" * (128 * 1024) + "FULL-END")
    assert engine.observe(row)["decision"] == "content_too_large"
    assert engine.status()["counts"] == {}
    assert engine.observe(snapshot())["decision"] == "queued"


def test_retries_are_bounded_and_timeout_remains_uncertain(tmp_path):
    clock = Clock()
    provider = FakeProvider([ProviderError("provider_timeout", uncertain=True)] * 3)
    engine = service(tmp_path, provider=provider, clock=clock)
    engine.observe(snapshot())
    for expected, gap in [("retry_wait", 30), ("retry_wait", 60), ("failed", 120)]:
        result = engine.dispatch_once()
        assert result["decision"] == expected
        assert result["uncertain"] is True
        if expected == "retry_wait":
            assert engine.dispatch_once()["decision"] == "idle"
        clock.value += gap
    assert engine.dispatch_once()["decision"] == "idle"
    assert len(provider.calls) == 3
    latest = engine.status()["latest"][0]
    assert latest["state"] == "failed" and latest["attempts"] == 3
    assert latest["error_code"] == "provider_timeout"


def test_permanent_rejection_and_unknown_exceptions_never_expose_secret(tmp_path):
    provider = FakeProvider([RuntimeError(TOKEN + " private path")])
    engine = service(tmp_path, provider=provider)
    engine.observe(snapshot())
    assert engine.dispatch_once()["decision"] == "retry_wait"
    assert TOKEN not in json.dumps(engine.status())
    provider.failures = [ProviderError("provider_rejected", retryable=False)]
    engine._clock.value += 30
    assert engine.dispatch_once()["decision"] == "failed"


def test_expired_inflight_lease_recovers_at_least_once_after_process_loss(tmp_path):
    clock = Clock()
    engine = service(tmp_path, clock=clock)
    engine.observe(snapshot())
    with sqlite3.connect(tmp_path / "outbox.sqlite") as connection:
        connection.execute(
            "UPDATE notification_outbox SET state='sending',attempts=1,lease_until=1010,uncertain=1"
        )
    restarted = service(tmp_path, clock=clock)
    assert restarted.dispatch_once()["decision"] == "idle"
    clock.value = 1010
    assert restarted.dispatch_once()["decision"] == "provider_accepted"
    assert restarted.status()["latest"][0]["attempts"] == 2


def test_network_dispatch_does_not_hold_observation_lock(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowProvider:
        def send(self, *_args):
            entered.set()
            assert release.wait(2)
            return ProviderReceipt()

    engine = service(tmp_path, provider=SlowProvider())
    engine.observe(snapshot())
    worker = threading.Thread(target=engine.dispatch_once)
    worker.start()
    try:
        assert entered.wait(2)
        assert engine.observe(snapshot(device="device-2"))["decision"] == "queued"
        assert engine.dispatch_once()["decision"] == "busy"
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()


def test_maintenance_advice_uses_actual_active_source_and_safe_generic_fallback():
    alarms = [
        {
            "rule_id": "rule",
            "level": "red",
            "start_seq": 256,
            "reason": "drivers: rms=2 (risk 0.9)",
        },
        {
            "rule_id": "rule",
            "level": "yellow",
            "start_seq": 512,
            "reason": "drivers: mean=1 (risk 0.2)",
        },
    ]
    states = [{"rule_id": "rule", "state": {"active_level": "red"}}]
    advice = maintenance_advice(alarms, states)
    assert any("RMS" in item for item in advice)
    assert not any("均值" in item for item in advice)
    assert any("人员按现场规程" in item for item in advice)
    missing = maintenance_advice([], states)
    assert any("来源记录缺失" in item for item in missing)
    fallback = maintenance_advice(
        [{**alarms[0], "reason": "drivers: rms=Inf (risk 0.9), arbitrary=1 (risk 1)"}],
        states,
    )
    assert any("采样质量" in item for item in fallback)


def test_pending_threshold_is_historical_and_policy_change_does_not_rewrite_it(
    tmp_path,
):
    engine = service(tmp_path, policy=NotificationPolicy(enabled=True, red_threshold=2))
    engine.observe(snapshot(2))
    engine.configure(replace(engine.policy, red_threshold=9))
    latest = engine.status()["latest"][0]
    assert latest["threshold"] == 2
    assert latest["next_attempt_at"] == 1000
    assert latest["updated_at"] == 1000


def test_two_services_share_atomic_source_cursor_without_double_enqueue(tmp_path):
    first = service(tmp_path)
    second = service(tmp_path)
    gate = threading.Barrier(2)
    decisions = []

    def observe(engine):
        gate.wait()
        decisions.append(engine.observe(snapshot())["decision"])

    workers = [
        threading.Thread(target=observe, args=(engine,)) for engine in (first, second)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3)
    assert sorted(decisions) == ["already_queued", "queued"]
    assert first.status()["counts"] == {"queued": 1}


def test_close_reports_unfinished_dispatcher_instead_of_claiming_success(tmp_path):
    engine = service(tmp_path)

    class UnfinishedThread:
        def join(self, timeout):
            assert timeout == 6

        def is_alive(self):
            return True

    engine._thread = UnfinishedThread()
    with pytest.raises(RuntimeError, match="notification_shutdown_incomplete"):
        engine.close()
    assert engine.status()["last_error"] == "notification_shutdown_incomplete"


def test_cooldown_snapshot_survives_restart_without_observing_old_run(tmp_path):
    clock = Clock()
    clock.value = 0
    first_provider = FakeProvider()
    first = service(tmp_path, clock=clock, provider=first_provider)
    assert first.observe(snapshot(1))["decision"] == "queued"
    assert first.dispatch_once()["decision"] == "provider_accepted"
    clock.value = 10
    pending = first.observe(snapshot(2, report_markdown="# Red2 FULL REPORT"))
    assert pending["decision"] == "queued" and pending["eligible_at"] == 600
    clock.value = 20
    complete_red3 = "# Red3 FULL START\n" + "完整正文\n" * 500 + "FULL END"
    merged = first.observe(
        snapshot(3, report_markdown=complete_red3, advice=("第三事件完整维护建议",))
    )
    assert merged == {"decision": "coalesced", "id": pending["id"], "eligible_at": 600}
    assert first.status()["counts"] == {"provider_accepted": 1, "queued": 1}
    first.close()
    # There is intentionally no observe(old_run) after reopening: only the durable
    # outbox can supply the full latest report when this old catalog no longer exists.
    restarted_provider = FakeProvider()
    restarted = NotificationService(
        tmp_path / "outbox.sqlite", transport=restarted_provider, clock=clock
    )
    restarted.configure(restarted.policy, TOKEN)
    clock.value = 599
    assert restarted.dispatch_once()["decision"] == "idle"
    assert not restarted_provider.calls
    clock.value = 600
    assert restarted.dispatch_once()["decision"] == "provider_accepted"
    body = restarted_provider.calls[0][2]
    assert complete_red3 in body and "第三事件完整维护建议" in body
    assert "# Red2 FULL REPORT" not in body
    assert "累计红色告警事件：3" in body
    clock.value = 601
    next_job = restarted.observe(snapshot(4))
    assert next_job["decision"] == "queued" and next_job["eligible_at"] == 1200
    assert restarted.dispatch_once()["decision"] == "idle"
    assert restarted.status()["counts"] == {"provider_accepted": 2, "queued": 1}


def test_cooldown_merge_failure_rolls_back_payload_and_source_cursor_together(tmp_path):
    clock = Clock()
    clock.value = 0
    engine = service(tmp_path, clock=clock)
    engine.observe(snapshot(1))
    engine.dispatch_once()
    clock.value = 10
    pending = engine.observe(snapshot(2, report_markdown="# Original RED2 report"))
    with sqlite3.connect(tmp_path / "outbox.sqlite") as connection:
        connection.execute(
            "CREATE TRIGGER deny_coalesce BEFORE UPDATE OF markdown ON notification_outbox BEGIN SELECT RAISE(ABORT,'private error'); END"
        )
    clock.value = 20
    with pytest.raises(NotificationStorageError, match="notification_storage_error"):
        engine.observe(snapshot(3, report_markdown="# New RED3 report"))
    with sqlite3.connect(tmp_path / "outbox.sqlite") as connection:
        source = connection.execute(
            "SELECT observed_count,enqueued_count,last_enqueued_at FROM notification_sources"
        ).fetchone()
        row = connection.execute(
            "SELECT red_count,markdown,next_attempt_at FROM notification_outbox WHERE id=?",
            (pending["id"],),
        ).fetchone()
        assert source == (2, 2, 600)
        assert row[0] == 2 and "# Original RED2 report" in row[1] and row[2] == 600
        connection.execute("DROP TRIGGER deny_coalesce")
    assert engine.observe(snapshot(3))["decision"] == "coalesced"


@pytest.mark.parametrize(
    "prior_state,attempts,uncertain",
    [("retry_wait", 1, 1), ("sending", 1, 1), ("provider_accepted", 1, 0)],
)
def test_cooldown_never_rewrites_attempted_or_uncertain_delivery(
    tmp_path, prior_state, attempts, uncertain
):
    clock = Clock()
    clock.value = 0
    engine = service(tmp_path, clock=clock)
    engine.observe(snapshot(1))
    engine.dispatch_once()
    clock.value = 10
    pending = engine.observe(snapshot(2, report_markdown="# Immutable RED2 report"))
    with sqlite3.connect(tmp_path / "outbox.sqlite") as connection:
        connection.execute(
            "UPDATE notification_outbox SET state=?,attempts=?,uncertain=? WHERE id=?",
            (prior_state, attempts, uncertain, pending["id"]),
        )
    clock.value = 20
    new = engine.observe(snapshot(3, report_markdown="# RED3 report"))
    assert new["decision"] == "queued" and new["id"] != pending["id"]
    assert new["eligible_at"] == 1200
    with sqlite3.connect(tmp_path / "outbox.sqlite") as connection:
        row = connection.execute(
            "SELECT red_count,markdown,state,attempts FROM notification_outbox WHERE id=?",
            (pending["id"],),
        ).fetchone()
    assert row[0] == 2 and "# Immutable RED2 report" in row[1]
    assert row[2:] == (prior_state, attempts)
