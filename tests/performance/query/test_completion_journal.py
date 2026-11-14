"""A blocked/failed evidence writer cannot hold HTTP terminal processing."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import threading

import pytest

from tools.query_load.accounting import ClientRecord
from tools.query_load import observation_runner
from tools.query_load.completion_journal import (
    CompletionJournal,
    CompletionJournalError,
)


class Evidence:
    def __init__(self, directory, *, block=False, fail=False):
        self.directory = directory
        self.rows = {"request_connection": []}
        self.errors = []
        self.records = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block, self.fail = block, fail

    def write(self, kind, value):
        if kind == "client":
            self.entered.set()
            if self.block:
                if not self.release.wait(timeout=10):
                    raise TimeoutError("test failed to release writer")
            if self.fail:
                raise OSError("private evidence failure text must not be copied")
        self.records.append((kind, deepcopy(value)))


def pair(index, *, started=True):
    record = ClientRecord(
        user_id=f"actor-{index}",
        run_id="run",
        device_id="device",
        started_monotonic=1.0,
        waiting_monotonic=1.1,
        completed_monotonic=1.2,
        status=200 if started else 0,
        server_request_id=f"r{index}" if started else "",
        response_bytes_base64="e30=",
    )
    mapping = {
        "request_id": record.server_request_id,
        "preparation": {
            "http_started": started,
            "send_started_ns": 10 if started else None,
        },
    }
    return record, mapping


def test_emit_is_bounded_nonblocking_and_freezes_records_while_writer_is_blocked(
    tmp_path,
):
    evidence = Evidence(tmp_path, block=True)
    journal = CompletionJournal(evidence, 2)
    first, mapping = pair(0)
    with ThreadPoolExecutor(max_workers=1) as producer:
        try:
            submitted = producer.submit(journal.emit, first, mapping)
            assert evidence.entered.wait(timeout=3)
            submitted.result(timeout=3)
            second = producer.submit(journal.emit, *pair(1))
            second.result(timeout=3)  # must not wait for the blocked disk writer
            first.status = 0
            mapping["preparation"]["send_started_ns"] = 999
            assert journal.facts()["submitted_pairs"] == 2
        finally:
            evidence.release.set()
            journal.finish()
    clients = [value for kind, value in evidence.records if kind == "client"]
    mappings = [
        value for kind, value in evidence.records if kind == "request_connection"
    ]
    assert len(clients) == len(mappings) == 2
    assert clients[0]["status"] == 200
    assert mappings[0]["preparation"]["send_started_ns"] == 10
    assert journal.facts()["drained"] is True
    assert journal.facts()["max_pending_pairs_observed"] <= 2


def test_writer_error_preserves_every_known_pair_without_retrying_partial_raw(tmp_path):
    evidence = Evidence(tmp_path, fail=True)
    journal = CompletionJournal(evidence, 3)
    for index in range(3):
        journal.emit(*pair(index))
    with pytest.raises(CompletionJournalError):
        journal.finish()
    facts = journal.facts()
    assert facts["errors"] == ["OSError"]
    assert facts["drained"] is True
    assert facts["fully_written_pairs"] == 0
    assert not evidence.records
    fallback = tmp_path / facts["fallback"]["path"]
    payload = fallback.read_bytes()
    saved = json.loads(payload)
    assert len(saved["items"]) == 3
    assert saved["partial_raw_write_may_exist"] is True
    assert saved["normal_raw_one_copy_claim_applies"] is False
    assert saved["quality_passed"] is False
    assert b"private evidence failure text" not in payload
    assert hashlib.sha256(payload).hexdigest() == facts["fallback"]["sha256"]
    with pytest.raises(CompletionJournalError):
        journal.finish()
    assert fallback.read_bytes() == payload


def test_unsent_attempt_is_never_published_as_an_actual_client_interval(tmp_path):
    evidence = Evidence(tmp_path)
    journal = CompletionJournal(evidence, 1)
    journal.emit(*pair(0, started=False))
    journal.finish()
    assert [kind for kind, _ in evidence.records] == [
        "client_attempt",
        "request_connection",
    ]
    assert not evidence.records[1][1]["preparation"]["http_started"]


def test_capacity_cannot_expand_beyond_the_planned_number_of_calls(tmp_path):
    evidence = Evidence(tmp_path)
    journal = CompletionJournal(evidence, 1)
    try:
        journal.emit(*pair(0))
        with pytest.raises(CompletionJournalError):
            journal.emit(*pair(1))
    finally:
        journal.finish()
    assert journal.facts()["submitted_pairs"] == 1


def test_real_wave_journal_failure_keeps_all_known_calls_and_fails_quality(
    tmp_path, monkeypatch
):
    original = observation_runner.WaveEvidence.write

    def fail_completed_record(self, kind, value, **kwargs):
        if self.wave_id == "wave-00001" and kind == "client":
            raise OSError("private injected journal disk failure")
        return original(self, kind, value, **kwargs)

    monkeypatch.setattr(observation_runner.WaveEvidence, "write", fail_completed_record)
    output = tmp_path / "real-failed-journal"
    summary = observation_runner.run_observation(
        output, concurrent=10, duration_seconds=60, max_waves=3
    )
    assert summary["completed_waves"] == 1
    assert summary["quality_passed"] is False
    assert summary["gate0_passed"] is False
    assert summary["stop_reason"] == "wave_quality_failed"
    assert summary["exit_code"] == 1
    report = json.loads((output / "wave-00001" / "report.json").read_bytes())
    facts = report["completion_journal"]
    assert facts["submitted_pairs"] == 10
    assert facts["fully_written_pairs"] == 0
    assert facts["drained"] is True
    assert facts["errors"] == ["OSError"]
    assert "completion_journal_OSError" in report["checks"]["stream"]
    payload = (output / "wave-00001" / facts["fallback"]["path"]).read_bytes()
    fallback = json.loads(payload)
    assert hashlib.sha256(payload).hexdigest() == facts["fallback"]["sha256"]
    assert len(fallback["items"]) == report["completed_requests"] == 10
    assert all(
        row["record"]["status"] == 200
        and row["mapping"]["preparation"]["http_started"] is True
        for row in fallback["items"]
    )
    raw = (output / "wave-00001" / "raw.ndjson").read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert not [row for row in rows if row["kind"] == "client"]
    assert (
        len(
            [
                row
                for row in rows
                if row["kind"] == "transport"
                and row["value"]["stage"] == "response_completed"
            ]
        )
        == 10
    )
    index = [
        json.loads(line) for line in (output / "index.ndjson").read_bytes().splitlines()
    ]
    assert hashlib.sha256(raw).hexdigest() == index[-1]["raw"]["sha256"]
    assert b"private injected journal disk failure" not in payload
