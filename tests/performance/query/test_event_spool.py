"""A killed observer or a failed publisher cannot erase known wave events."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from tools.query_load import event_spool
from tools.query_load.event_spool import SpoolError, WaveEventSpool


def origin(index):
    return {
        "stage": "server_received",
        "request_id": f"request-{index}",
        "process_id": 1234,
        "monotonic_ns": 10000 + index,
        "nested": {"value": index},
    }


def values(recovery):
    return [message["event"] for message in recovery.iter_messages()]


class MappingSpy:
    def __init__(self, inner):
        self.inner = inner
        self.flushes = self.closes = 0

    def __getitem__(self, key):
        return self.inner[key]

    def __setitem__(self, key, value):
        self.inner[key] = value

    def flush(self):
        self.flushes += 1
        return self.inner.flush()

    def close(self):
        self.closes += 1
        return self.inner.close()


def test_normal_append_freezes_origin_without_ipc_flush_file_write_or_thread(
    tmp_path, monkeypatch
):
    spool = WaveEventSpool(tmp_path, "wave-00001", max_bytes=4096)
    mapping = MappingSpy(spool._mapping)
    spool._mapping = mapping
    original = origin(0)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("normal append performed durability or thread work")

    with monkeypatch.context() as patcher:
        patcher.setattr(event_spool.os, "fsync", forbidden)
        patcher.setattr(event_spool.threading, "Thread", forbidden)
        spool.append("transport", original)
        original["nested"]["value"] = 999
        spool.append("resource", {"rss_bytes": 500, "process_id": 1234})
        assert mapping.flushes == 0
        assert mapping.closes == 0
        assert spool.path.stat().st_size == 4096
    receipt = spool.seal()
    assert mapping.flushes == mapping.closes == 1
    assert receipt["sealed"] is receipt["fsync_succeeded"] is True
    assert receipt["incomplete"] is False
    assert receipt["bytes"] < 4096
    assert hashlib.sha256(spool.path.read_bytes()).hexdigest() == receipt["sha256"]
    assert values(spool) == [origin(0), {"rss_bytes": 500, "process_id": 1234}]
    assert spool.seal() == receipt
    recovered = WaveEventSpool.recover(spool.path, "wave-00001")
    assert recovered.facts()["incomplete"] is False
    assert recovered.facts()["fsync_independently_verified"] is False


def test_multiple_producers_have_one_recoverable_order_and_no_frame_interleaving(
    tmp_path,
):
    spool = WaveEventSpool(tmp_path, "wave-00002", max_bytes=65536)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda number: spool.append("transport", origin(number)), range(80)
            )
        )
    spool.seal()
    saved = values(spool)
    assert len(saved) == 80
    assert {row["request_id"] for row in saved} == {f"request-{n}" for n in range(80)}
    assert all(row == origin(int(row["request_id"].split("-")[-1])) for row in saved)


def test_overflow_keeps_partial_original_and_rejected_and_future_known_events(tmp_path):
    spool = WaveEventSpool(tmp_path, "wave-00003", max_bytes=1024)
    spool.append("transport", origin(0))
    oversized = {"request_id": "large", "metadata": "x" * 3000}
    with pytest.raises(SpoolError):
        spool.append("transport", oversized)
    partial = spool.path.read_bytes()
    for number in (2, 3):
        with pytest.raises(SpoolError):
            spool.append("transport", origin(number))
    with pytest.raises(SpoolError):
        spool.seal()
    assert spool.path.read_bytes() == partial  # never overwrite the partial frame
    saved = values(spool)
    assert saved == [origin(0), oversized, origin(2), origin(3)]
    facts = spool.facts()
    assert facts["capacity_bytes"] == facts["bytes"] == 1024
    assert facts["attempts"] == 4
    assert facts["emergency"]["committed_messages"] == 3
    assert facts["emergency"]["bytes"] > 3000
    assert facts["incomplete"] is True
    recovered = WaveEventSpool.recover(spool.path, "wave-00003")
    assert recovered.facts()["committed_frames"] == 1
    assert recovered.facts()["recovered_messages"] == 4
    assert recovered.facts()["incomplete"] is True


def test_mmap_error_after_commit_marker_deduplicates_only_exact_uncertain_copy(
    tmp_path, monkeypatch
):
    spool = WaveEventSpool(tmp_path, "wave-00004", max_bytes=4096)
    put = spool._put

    def uncertain_commit(offset, payload):
        put(offset, payload)
        if payload == event_spool._COMMIT:
            raise OSError("mapping write reported an uncertain commit")

    monkeypatch.setattr(spool, "_put", uncertain_commit)
    with pytest.raises(SpoolError):
        spool.append("transport", origin(0))
    with pytest.raises(SpoolError):
        spool.append("transport", origin(1))
    with pytest.raises(SpoolError):
        spool.seal()
    recovery = WaveEventSpool.recover(spool.path, "wave-00004")
    assert values(recovery) == [origin(0), origin(1)]
    assert recovery.facts()["uncertain_commit_duplicates"] == 1
    assert recovery.facts()["incomplete"] is True


def test_sealed_artifact_survives_failure_during_later_ipc_publication(tmp_path):
    spool = WaveEventSpool(tmp_path, "wave-00005")
    for number in range(3):
        spool.append("transport", origin(number))
    receipt = spool.seal()
    published = []

    def failed_publish(message):
        published.append(message)
        raise BrokenPipeError("peer exited after fsync, before publication")

    with pytest.raises(BrokenPipeError):
        for message in spool.iter_messages():
            failed_publish(message)
    assert len(published) == 1
    recovery = WaveEventSpool.recover(spool.path, "wave-00005")
    assert values(recovery) == [origin(n) for n in range(3)]
    assert receipt["fsync_succeeded"] is True
    assert hashlib.sha256(spool.path.read_bytes()).hexdigest() == receipt["sha256"]


def test_real_process_killed_before_seal_preserves_committed_prefix_and_partial_tail(
    tmp_path,
):
    source = """
import json,sys
from tools.query_load.event_spool import WaveEventSpool
root=sys.argv[1]
spool=WaveEventSpool(root, 'wave-killed', max_bytes=4096)
for index in range(3):
    spool.append('transport', {'request_id':str(index),'process_id':1234,'monotonic_ns':index+1})
put=spool._put
def checkpoint(offset,payload):
    put(offset,payload)
    print(json.dumps({'checkpoint':'partial_frame_header_written','path':spool.path.name}),flush=True)
    sys.stdin.buffer.read(1)
spool._put=checkpoint
spool.append('transport', {'request_id':'last'})
raise AssertionError('parent must kill before seal')
"""
    environment = os.environ.copy()
    repository = Path(__file__).resolve().parents[3]
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(repository / "src"), str(repository)]
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", source, str(tmp_path)],
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        ready = executor.submit(process.stdout.readline).result(timeout=10)
        assert json.loads(ready)["checkpoint"] == "partial_frame_header_written"
        process.kill()
        process.wait(timeout=10)
        if os.name == "posix":
            assert process.returncode == -signal.SIGKILL
        else:
            assert process.returncode != 0  # TerminateProcess, not POSIX SIGKILL.
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        executor.shutdown(wait=True)
        process.communicate(timeout=10)
    path = tmp_path / "wave-killed.events.spool"
    before = path.read_bytes()
    recovery = WaveEventSpool.recover(path, "wave-killed")
    assert [row["request_id"] for row in values(recovery)] == ["0", "1", "2"]
    assert recovery.facts()["committed_frames"] == 3
    assert recovery.facts()["incomplete"] is True
    assert recovery.facts()["seal_marker_present"] is False
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "name", ["", ".", "..", "../wave", "w/x", "w\\x", "中", "a" * 65]
)
def test_bad_wave_names_do_not_create_files(tmp_path, name):
    with pytest.raises(ValueError):
        WaveEventSpool(tmp_path, name)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "capacity", [True, False, 0, -1, 511, 1024.0, 16 * 1024 * 1024 + 1]
)
def test_byte_capacity_is_strict_and_cannot_expand_original_bound(tmp_path, capacity):
    with pytest.raises(ValueError):
        WaveEventSpool(tmp_path, "wave-cap", max_bytes=capacity)


def test_reopen_and_cross_wave_cannot_replace_or_mislabel_saved_evidence(tmp_path):
    spool = WaveEventSpool(tmp_path, "wave-00006", max_bytes=4096)
    spool.append("marker", {"marker": "begin"})
    with pytest.raises(FileExistsError):
        WaveEventSpool(tmp_path, "wave-00006")
    spool.seal()
    before = spool.path.read_bytes()
    with pytest.raises(SpoolError, match="wave mismatch"):
        WaveEventSpool.recover(spool.path, "another-wave")
    assert spool.path.read_bytes() == before


@pytest.mark.parametrize(
    "kind,event",
    [
        ("", {"x": 1}),
        (1, {"x": 1}),
        ("transport", []),
        ("transport", {"number": float("nan")}),
        ("transport", {"number": float("inf")}),
        ("transport", {1: "not a string key"}),
        ("transport", {"object": object()}),
    ],
)
def test_malformed_events_are_sticky_projected_without_repr_then_future_is_kept(
    tmp_path, kind, event
):
    spool = WaveEventSpool(tmp_path, "wave-bad", max_bytes=4096)
    with pytest.raises(SpoolError):
        spool.append(kind, event)
    with pytest.raises(SpoolError):
        spool.append("transport", origin(1))
    with pytest.raises(SpoolError):
        spool.seal()
    saved = list(spool.iter_messages())
    assert saved[-1]["event"] == origin(1)
    assert "spool_rejection" in saved[0]
    assert spool.facts()["incomplete"] is True
    assert b" object at 0x" not in spool.emergency_path.read_bytes()


@pytest.mark.parametrize("key", ["token", "Authorization", "payload", "windows_cursor"])
def test_sensitive_fields_are_never_copied_into_original_or_emergency(tmp_path, key):
    spool = WaveEventSpool(tmp_path, "wave-secret", max_bytes=4096)
    with pytest.raises(SpoolError):
        spool.append("transport", {"request_id": "known", key: "SECRET-NOT-RETAINABLE"})
    with pytest.raises(SpoolError):
        spool.seal()
    assert b"SECRET-NOT-RETAINABLE" not in spool.path.read_bytes()
    assert b"SECRET-NOT-RETAINABLE" not in spool.emergency_path.read_bytes()
    assert list(spool.iter_messages())[0]["event"]["request_id"] == "known"
    assert spool.facts()["incomplete"] is True


def test_late_after_seal_and_future_events_survive_without_rewriting_sealed_file(
    tmp_path,
):
    spool = WaveEventSpool(tmp_path, "wave-late", max_bytes=4096)
    spool.append("transport", origin(0))
    spool.seal()
    before = spool.path.read_bytes()
    for index in (1, 2):
        with pytest.raises(SpoolError):
            spool.append("transport", origin(index))
    with pytest.raises(SpoolError):
        spool.seal()
    assert spool.path.read_bytes() == before
    assert values(spool) == [origin(0), origin(1), origin(2)]
    assert spool.facts()["sealed"] is True
    assert spool.facts()["incomplete"] is True


def test_close_does_not_seal_and_is_idempotent_but_later_attempt_is_retained(tmp_path):
    spool = WaveEventSpool(tmp_path, "wave-close", max_bytes=4096)
    spool.append("transport", origin(0))
    spool.close()
    spool.close()
    assert spool.path.stat().st_size == 4096
    assert spool.facts()["sealed"] is spool.facts()["fsync_succeeded"] is False
    with pytest.raises(SpoolError):
        spool.append("transport", origin(1))
    assert values(spool) == [origin(0), origin(1)]


def test_fsync_failure_preserves_file_and_future_evidence_then_stays_failed(
    tmp_path, monkeypatch
):
    spool = WaveEventSpool(tmp_path, "wave-sync", max_bytes=4096)
    spool.append("transport", origin(0))

    def broken(_fd):
        raise OSError("durability device failed")

    monkeypatch.setattr(event_spool.os, "fsync", broken)
    with pytest.raises(SpoolError):
        spool.seal()
    assert spool.path.stat().st_size == 4096
    with pytest.raises(SpoolError):
        spool.append("transport", origin(1))
    assert values(spool) == [origin(0), origin(1)]
    assert spool.facts()["sealed"] is spool.facts()["fsync_succeeded"] is False
    assert spool.facts()["incomplete"] is True


def test_emergency_write_failure_is_explicit_and_does_not_overwrite_existing_file(
    tmp_path,
):
    spool = WaveEventSpool(tmp_path, "wave-emergency", max_bytes=512)
    spool.emergency_path.write_bytes(b"prior evidence\n")
    with pytest.raises(SpoolError):
        spool.append("transport", {"metadata": "x" * 1000})
    assert spool.emergency_path.read_bytes() == b"prior evidence\n"
    facts = spool.facts()
    assert "emergency_FileExistsError" in facts["errors"]
    assert facts["incomplete"] is True
    with pytest.raises(SpoolError):
        spool.close()


def test_tampered_committed_frame_is_not_presented_as_valid_prefix(tmp_path):
    spool = WaveEventSpool(tmp_path, "wave-tamper", max_bytes=4096)
    spool.append("transport", origin(0))
    spool.append("transport", origin(1))
    spool.seal()
    payload = spool.path.read_bytes().replace(b"request-1", b"request-9")
    spool.path.write_bytes(payload)
    recovery = WaveEventSpool.recover(spool.path, "wave-tamper")
    assert values(recovery) == [origin(0)]
    assert recovery.facts()["incomplete"] is True


@pytest.mark.parametrize("characters", [5000, 5600])
def test_small_frame_fast_path_has_a_strict_scratch_bound_and_large_json_streams(
    tmp_path, monkeypatch, characters
):
    spool = WaveEventSpool(tmp_path, "wave-fast", max_bytes=131072)
    chunks, dumps = event_spool._chunks, event_spool.json.dumps
    streamed = []
    encoded_sizes = []

    def observe_chunks(value):
        streamed.append(type(value).__name__)
        yield from chunks(value)

    def observe_dumps(*args, **kwargs):
        encoded = dumps(*args, **kwargs)
        encoded_sizes.append(len(encoded.encode("ascii")))
        return encoded

    monkeypatch.setattr(event_spool, "_chunks", observe_chunks)
    monkeypatch.setattr(event_spool.json, "dumps", observe_dumps)
    expected = {"metadata": "\U0001f642" * characters}
    spool.append("resource", expected)
    assert bool(streamed) is (characters == 5600)
    assert max(encoded_sizes) <= 65536
    spool.seal()
    assert values(spool) == [expected]


def test_failed_seal_then_close_does_not_prevent_future_known_event_retention(tmp_path):
    spool = WaveEventSpool(tmp_path, "wave-future", max_bytes=512)
    rejected = {"metadata": "x" * 2000}
    with pytest.raises(SpoolError):
        spool.append("transport", rejected)
    with pytest.raises(SpoolError):
        spool.seal()
    before = spool.path.read_bytes()
    for index in (1, 2):
        with pytest.raises(SpoolError):
            spool.append("transport", origin(index))
        with pytest.raises(SpoolError):
            spool.close()
    assert spool.path.read_bytes() == before
    assert values(spool) == [rejected, origin(1), origin(2)]


def test_partial_emergency_write_preserves_tail_and_recovers_later_known_rows(
    tmp_path, monkeypatch
):
    spool = WaveEventSpool(tmp_path, "wave-partial", max_bytes=512)
    write = spool._emergency_write
    calls = 0

    def fail_once(payload):
        nonlocal calls
        calls += 1
        if calls == 5:
            write(payload[:2])
            raise OSError("one partial failed filesystem write")
        write(payload)

    monkeypatch.setattr(spool, "_emergency_write", fail_once)
    with pytest.raises(SpoolError):
        spool.append("transport", {"metadata": "x" * 2000})
    partial = spool.emergency_path.read_bytes()
    for index in (1, 2):
        with pytest.raises(SpoolError):
            spool.append("transport", origin(index))
    assert spool.emergency_path.read_bytes().startswith(partial)
    recovery = WaveEventSpool.recover(spool.path, "wave-partial")
    assert values(recovery) == [origin(1), origin(2)]
    assert "invalid_or_partial_emergency" in recovery.facts()["errors"]
    assert "emergency_sequence_gap" in recovery.facts()["errors"]
    assert spool.facts()["incomplete"] is recovery.facts()["incomplete"] is True
    with pytest.raises(SpoolError):
        spool.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("serialization_complete", 1),
        ("sequence", True),
        ("wave_id", "wrong"),
        ("format", "unknown"),
    ],
)
def test_emergency_type_and_identity_tampering_fails_recovery(tmp_path, field, value):
    spool = WaveEventSpool(tmp_path, "wave-types", max_bytes=512)
    with pytest.raises(SpoolError):
        spool.append("transport", {"metadata": "x" * 2000})
    with pytest.raises(SpoolError):
        spool.close()
    row = json.loads(spool.emergency_path.read_bytes())
    row[field] = value
    spool.emergency_path.write_bytes((json.dumps(row) + "\n").encode())
    recovery = WaveEventSpool.recover(spool.path, "wave-types")
    assert values(recovery) == []
    assert recovery.facts()["incomplete"] is True


def test_interrupted_serialization_is_retained_then_original_interrupt_propagates(
    tmp_path, monkeypatch
):
    spool = WaveEventSpool(tmp_path, "wave-interrupt", max_bytes=4096)
    validate = event_spool._validate

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt("serialization interrupted")

    with monkeypatch.context() as patcher:
        patcher.setattr(event_spool, "_validate", interrupted)
        with pytest.raises(KeyboardInterrupt):
            spool.append("transport", origin(0))
    assert event_spool._validate is validate
    with pytest.raises(SpoolError):
        spool.append("transport", origin(1))
    with pytest.raises(SpoolError):
        spool.seal()
    saved = list(spool.iter_messages())
    assert saved[0]["event"] == origin(0)
    assert saved[0]["spool_rejection"] == "KeyboardInterrupt"
    assert saved[1]["event"] == origin(1)


def test_hash_or_stat_failure_is_reported_incomplete_without_fabricated_zero_bytes(
    tmp_path, monkeypatch
):
    spool = WaveEventSpool(tmp_path, "wave-unreadable", max_bytes=4096)
    spool.append("transport", origin(0))

    def unreadable(_path):
        raise OSError("evidence hash is unavailable")

    with monkeypatch.context() as patcher:
        patcher.setattr(event_spool, "_sha_file", unreadable)
        with pytest.raises(SpoolError):
            spool.seal()
        facts = spool.facts()
        assert facts["sha256"] is None
        assert facts["incomplete"] is True
    spool.path.unlink()
    facts = spool.facts()
    assert facts["bytes"] is None
    assert facts["incomplete"] is True
