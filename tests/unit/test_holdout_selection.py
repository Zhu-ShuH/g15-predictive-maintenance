"""Selection failure boundaries with injected, already-public constants only."""

import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location(
    "holdout_selection", Path(__file__).parents[2] / "tools" / "holdout_selection.py"
)
selection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selection)
Ledger = selection.Ledger
SelectionError = selection.SelectionError
RECEIPT = "https://example.org/verified-test-receipt"


def git(source, *args):
    return subprocess.run(
        ["git", "-c", f"safe.directory={source.as_posix()}", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def config(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--quiet")
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
        "--quiet",
    )
    commit = git(source, "rev-parse", "HEAD")
    audit = json.dumps(
        {"source_commit": commit, "forbidden_values": [7], "unknowns": []}
    ).encode()
    (tmp_path / "audit.json").write_bytes(audit)
    return {
        "source_commit": commit,
        "method_blob": "c" * 40,
        "audit_sha256": hashlib.sha256(audit).hexdigest(),
        "approval_receipt": RECEIPT,
        "required_declarants": ["B2", "C1", "A"],
        "excluded_seeds": [7],
        "unresolved_sources": [],
    }


@pytest.fixture
def ledger(tmp_path, config):
    return Ledger.create(tmp_path / "selection.sqlite", config, tmp_path / "audit.json")


def draw(ledger, seed):
    return ledger.draw(
        ledger.path.parent / "source",
        ledger.path.parent / "audit.json",
        randbelow=lambda _: seed - 1,
    )


def draw_with(ledger, rng):
    return ledger.draw(
        ledger.path.parent / "source", ledger.path.parent / "audit.json", randbelow=rng
    )


def decision(ledger, action, slot=1, **details):
    current = ledger.status()["slots"][str(slot)]
    return ledger.record(
        action,
        slot,
        current["attempt"],
        current["seed"],
        RECEIPT,
        **details,
    )


def accept(ledger, slot=1):
    for actor in ("B2", "C1", "A"):
        decision(ledger, "declare", slot, actor=actor, contacted=False)
    return decision(ledger, "accept", slot)


def test_intent_committed_before_rng_and_pending_blocks_next_slot(ledger):
    def rng(bound):
        assert bound == 2**32 - 1
        observed = Ledger(ledger.path).status()
        assert observed["paused"]
        assert observed["slots"]["1"]["status"] == "intent"
        assert observed["slots"]["1"]["used"] == 0
        return 42 - 1

    result = draw_with(ledger, rng)
    assert result["waiting"]
    assert result["slots"]["1"]["used"] == 1
    with pytest.raises(SelectionError, match="waiting"):
        draw(Ledger(ledger.path), 43)
    assert ledger.status()["slots"]["2"]["used"] == 0


def test_rng_exception_survives_restart_and_never_redraws(ledger):
    def fail(_):
        raise OSError("entropy unavailable")

    with pytest.raises(OSError):
        draw_with(ledger, fail)
    reopened = Ledger(ledger.path)
    with pytest.raises(SelectionError, match="paused"):
        draw(reopened, 42)
    assert reopened.status()["event_count"] == 2


def test_return_persistence_failure_leaves_unknown_intent(ledger, monkeypatch):
    original = Ledger._append

    def fail_return(conn, event):
        if event["event_type"] == "draw_returned":
            raise sqlite3.OperationalError("disk full")
        original(conn, event)

    monkeypatch.setattr(Ledger, "_append", staticmethod(fail_return))
    with pytest.raises(sqlite3.OperationalError):
        draw(ledger, 42)
    assert Ledger(ledger.path).status()["paused"]
    assert ledger.status()["slots"]["1"]["status"] == "intent"


def test_accept_requires_each_real_receipt_and_cannot_erase_contact(ledger):
    draw(ledger, 42)
    with pytest.raises(SelectionError, match="missing"):
        decision(ledger, "accept")
    decision(ledger, "declare", actor="B2", contacted=True)
    decision(ledger, "declare", actor="B2", contacted=False)
    with pytest.raises(SelectionError, match="acceptable"):
        accept(ledger)
    assert ledger.status()["paused"]
    assert ledger.status()["slots"]["1"]["used"] == 1


def test_two_candidates_require_order_and_distinctness(ledger):
    draw(ledger, 42)
    accept(ledger)
    result = draw(ledger, 42)
    assert result["slots"]["2"]["status"] == "invalid"
    decision(ledger, "reject", slot=2, reason="duplicate")
    assert ledger.status()["slots"]["1"]["status"] == "accepted"
    draw(ledger, 43)
    result = accept(ledger, slot=2)
    assert result["complete"]
    assert [s["used"] for s in result["slots"].values()] == [1, 2]
    with pytest.raises(SelectionError, match="complete"):
        draw(ledger, 7)


def test_third_candidate_waits_and_can_be_accepted(ledger):
    for _ in range(2):
        draw(ledger, 7)
        decision(ledger, "reject", reason="excluded")
    result = draw(ledger, 42)
    assert result["waiting"] and not result["exhausted"]
    result = accept(ledger)
    assert result["slots"]["1"]["used"] == 3
    assert result["slots"]["1"]["status"] == "accepted"


def test_third_rejection_stops_all_slots_and_restart_does_not_reset(ledger):
    for _ in range(3):
        draw(ledger, 7)
        decision(ledger, "reject", reason="excluded")
    reopened = Ledger(ledger.path)
    assert reopened.status()["exhausted"]
    with pytest.raises(SelectionError, match="paused"):
        draw(reopened, 42)
    assert reopened.status()["slots"]["2"]["used"] == 0


def test_revocation_preserves_budget_and_excludes_old_value(ledger):
    draw(ledger, 42)
    accept(ledger)
    decision(ledger, "declare", actor="C1", contacted=True)
    with pytest.raises(SelectionError, match="revoke"):
        decision(ledger, "reject", reason="contact")
    decision(ledger, "revoke", reason="contact")
    result = draw(ledger, 42)
    assert result["slots"]["1"]["status"] == "invalid"
    assert result["slots"]["1"]["used"] == 2
    assert sum(e["event_type"] == "accept" for e in result["events"]) == 1


def test_revocation_of_third_return_pauses_but_keeps_other_disclosures(ledger):
    for _ in range(2):
        draw(ledger, 7)
        decision(ledger, "reject", reason="excluded")
    draw(ledger, 42)
    accept(ledger)
    draw(ledger, 43)
    accept(ledger, slot=2)
    decision(ledger, "revoke", reason="late contact")
    result = decision(ledger, "declare", slot=2, actor="B2", contacted=True)
    assert result["paused"] and result["exhausted"]
    assert result["slots"]["2"]["used"] == 1


@pytest.mark.parametrize("changed", ["source", "audit", "dirty"])
def test_version_mismatch_precedes_intent_and_rng(ledger, changed):
    source = ledger.path.parent / "source"
    if changed == "source":
        git(
            source,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.org",
            "commit",
            "--allow-empty",
            "-m",
            "changed",
            "--quiet",
        )
    elif changed == "audit":
        (ledger.path.parent / "audit.json").write_text("{}", encoding="utf-8")
    else:
        (source / "untracked.txt").write_text("must stop", encoding="utf-8")

    def forbidden(_):
        raise AssertionError("RNG must not run")

    with pytest.raises(SelectionError, match="mismatch|clean"):
        draw_with(ledger, forbidden)
    assert ledger.status()["event_count"] == 1


def test_invalid_receipt_and_wrong_attempt_do_not_append(ledger):
    draw(ledger, 42)
    with pytest.raises(SelectionError, match="HTTPS"):
        ledger.record(
            "declare", 1, 1, 42, "local assertion", actor="B2", contacted=False
        )
    with pytest.raises(SelectionError, match="attempt"):
        ledger.record("declare", 1, 2, 42, RECEIPT, actor="B2", contacted=False)
    assert ledger.status()["event_count"] == 3


def test_ledger_is_append_only_and_cannot_reinitialize(ledger, config):
    with pytest.raises(FileExistsError):
        Ledger.create(ledger.path, config, ledger.path.parent / "audit.json")
    with sqlite3.connect(ledger.path) as conn:
        for statement in (
            "DELETE FROM events",
            "UPDATE events SET body='{}'",
            "INSERT OR REPLACE INTO events SELECT * FROM events",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append only"):
                conn.execute(statement)


def test_corrupt_record_stops_before_rng(ledger):
    with sqlite3.connect(ledger.path) as conn:
        conn.execute("DROP TRIGGER no_update")
        conn.execute("UPDATE events SET body=replace(body,'B2','XX') WHERE seq=1")
    with pytest.raises(SelectionError, match="hash"):
        draw(ledger, 42)


def test_unknown_sources_prevent_creation(tmp_path, config):
    config["unresolved_sources"] = ["dynamic source"]
    path = tmp_path / "blocked.sqlite"
    with pytest.raises(SelectionError, match="unresolved"):
        Ledger.create(path, config, tmp_path / "audit.json")
    assert not path.exists()


def test_missing_ledger_is_not_recreated(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(SelectionError, match="missing"):
        Ledger(path).status()
    assert not path.exists()


@pytest.mark.parametrize("changed", ["exclusions", "digest", "source", "unknown"])
def test_init_checks_audit_content_not_only_operator_claim(tmp_path, config, changed):
    if changed == "exclusions":
        config["excluded_seeds"] = []
    elif changed == "digest":
        config["audit_sha256"] = "d" * 64
    elif changed == "source":
        config["source_commit"] = "d" * 40
    else:
        audit = json.loads((tmp_path / "audit.json").read_bytes())
        audit["unknowns"] = ["unresolved expression"]
        raw = json.dumps(audit).encode()
        (tmp_path / "audit.json").write_bytes(raw)
        config["audit_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(SelectionError):
        Ledger.create(tmp_path / "blocked.sqlite", config, tmp_path / "audit.json")
    assert not (tmp_path / "blocked.sqlite").exists()


def test_contact_disclosure_during_rng_preserves_original_occupancy(ledger):
    draw(ledger, 42)
    accept(ledger)

    def concurrent(_):
        decision(ledger, "declare", actor="C1", contacted=True)
        return 42 - 1

    result = draw_with(ledger, concurrent)
    assert result["slots"]["2"]["status"] == "invalid"
    decision(ledger, "revoke", reason="contact")
    with pytest.raises(SelectionError, match="acceptable"):
        decision(ledger, "accept", slot=2)
    assert ledger.status()["slots"]["2"]["used"] == 1
