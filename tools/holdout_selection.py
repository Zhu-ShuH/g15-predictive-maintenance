"""Durable two-slot candidate ledger; never generates simulation data.

Receipts are externally verified by the operator: this tool records links, not
signatures or independent approval. Preserve and publish the ledger and its tail
hash; SQLite triggers/hash chains do not protect against file replacement or
tail deletion by an owner. Never start another ledger to reset a spent budget.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
from typing import Callable, Iterator
from urllib.parse import urlparse

MAX_SEED = 2**32 - 1
ZERO_HASH = "0" * 64


class SelectionError(ValueError):
    """An ambiguous or disallowed selection transition; do not retry a draw."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SelectionError(message)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _receipt(value: str) -> None:
    _require(isinstance(value, str), "HTTPS receipt required")
    parsed = urlparse(value)
    _require(parsed.scheme == "https" and bool(parsed.netloc), "HTTPS receipt required")


def _valid_seed(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_SEED


def _configuration(config: dict) -> None:
    _require(isinstance(config, dict), "invalid configuration")
    for field, length in (
        ("source_commit", 40),
        ("method_blob", 40),
        ("audit_sha256", 64),
    ):
        _require(
            isinstance(config.get(field), str)
            and re.fullmatch(rf"[0-9a-f]{{{length}}}", config[field]) is not None,
            f"invalid {field}",
        )
    _receipt(config.get("approval_receipt", ""))
    people = config.get("required_declarants")
    _require(
        isinstance(people, list)
        and all(isinstance(p, str) and p.strip() == p and p for p in people)
        and len(people) == len(set(people))
        and {"B2", "C1"}.issubset(people)
        and len(people) >= 3,
        "require B2, C1, executor and all generators/custodians",
    )
    excluded = config.get("excluded_seeds")
    _require(
        isinstance(excluded, list)
        and all(_valid_seed(v) for v in excluded)
        and len(excluded) == len(set(excluded)),
        "invalid exclusion set",
    )
    _require(config.get("unresolved_sources") == [], "unresolved audit sources")
    raw = config.get("audit_document")
    _require(isinstance(raw, str), "audit document required")
    _require(
        hashlib.sha256(raw.encode("utf-8")).hexdigest() == config["audit_sha256"],
        "audit digest mismatch",
    )
    audit = json.loads(raw)
    _require(
        isinstance(audit, dict)
        and audit.get("source_commit") == config["source_commit"],
        "audit source mismatch",
    )
    _require(audit.get("unknowns") == [], "unresolved audit sources")
    values = audit.get("forbidden_values")
    _require(
        isinstance(values, list) and all(_valid_seed(v) for v in values),
        "invalid audited exclusions",
    )
    _require(sorted(values) == sorted(excluded), "audit exclusions mismatch")


def _verify_source(source: Path, expected: str) -> None:
    """Check the actual checkout, including untracked files, before any RNG call."""
    source = source.resolve()
    command = ["git", "-c", f"safe.directory={source.as_posix()}", "-C", str(source)]
    head = subprocess.run(
        command + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    _require(head == expected, "source commit mismatch")
    dirty = subprocess.run(
        command + ["status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    _require(not dirty, "source checkout is not clean")


def _state(config: dict, events: list[dict]) -> dict:
    """Replay all events; derive counts exclusively from returned draws."""
    _configuration(config)
    slots = {
        str(i): {
            "used": 0,
            "attempt": 0,
            "seed": None,
            "status": "empty",
            "declarations": {},
            "confirmed": False,
        }
        for i in (1, 2)
    }
    excluded = set(config["excluded_seeds"])
    exhausted = False
    for event in events:
        _require(
            event.get("method_blob") == config["method_blob"], "method version mismatch"
        )
        slot_id = event.get("slot_id")
        _require(type(slot_id) is int and slot_id in (1, 2), "invalid slot")
        slot = slots[str(slot_id)]
        kind = event.get("event_type")
        attempt = event.get("attempt_no")
        _require(type(attempt) is int, "invalid attempt")
        if kind == "draw_intent":
            _require(not exhausted, "draw after exhausted slot")
            _require(
                all(s["status"] in ("empty", "accepted") for s in slots.values()),
                "draw while another candidate/intent is pending",
            )
            empty = [int(k) for k, s in slots.items() if s["status"] == "empty"]
            _require(bool(empty) and slot_id == min(empty), "wrong slot order")
            _require(attempt == slot["used"] + 1 <= 3, "invalid draw budget")
            _require(event.get("seed") is None, "intent already contains a seed")
            slot.update(
                attempt=attempt,
                seed=None,
                status="intent",
                declarations={},
                confirmed=False,
            )
            continue
        _require(attempt == slot["attempt"], "event for a different attempt")
        seed = event.get("seed")
        if kind == "draw_returned":
            _require(slot["status"] == "intent", "duplicate or missing draw intent")
            _require(type(seed) is int, "noninteger RNG return; audit required")
            slot["used"] += 1
            occupied = {
                s["seed"]
                for s in slots.values()
                if s["status"] not in ("empty", "intent")
            }
            invalid = not _valid_seed(seed) or seed in excluded or seed in occupied
            slot.update(seed=seed, status="invalid" if invalid else "pending")
            continue
        _require(seed == slot["seed"] and type(seed) is int, "candidate mismatch")
        _receipt(event.get("receipt", ""))
        if kind == "declare":
            _require(
                slot["status"] in ("pending", "accepted", "conflict"), "no candidate"
            )
            actor = event.get("actor")
            _require(actor in config["required_declarants"], "unknown declarant")
            contacted = event.get("contacted")
            _require(type(contacted) is bool, "contacted must be boolean")
            slot["declarations"][actor] = {
                "contacted": contacted,
                "receipt": event["receipt"],
            }
            if contacted:
                slot["status"] = "conflict"
            # A later denial cannot erase a contact disclosure; A must reject/revoke.
        elif kind == "accept":
            _require(slot["status"] == "pending", "candidate is not acceptable")
            occupied = {
                s["seed"]
                for key, s in slots.items()
                if key != str(slot_id) and s["status"] not in ("empty", "intent")
            }
            _require(
                seed not in excluded and seed not in occupied,
                "candidate became excluded or occupied",
            )
            _require(
                set(slot["declarations"]) == set(config["required_declarants"])
                and all(not d["contacted"] for d in slot["declarations"].values()),
                "missing or conflicting personal declarations",
            )
            slot["status"] = "accepted"
            slot["confirmed"] = True
        elif kind in ("reject", "revoke"):
            _require(bool(event.get("reason")), "decision reason required")
            allowed = (
                ("accepted", "conflict")
                if kind == "revoke"
                else ("pending", "invalid", "conflict")
            )
            _require(slot["status"] in allowed, "invalid rejection/revocation")
            _require(
                slot["confirmed"] == (kind == "revoke"),
                "use revoke for a previously accepted candidate",
            )
            excluded.add(seed)
            slot["status"] = "empty"
            if slot["used"] == 3:
                exhausted = True
        else:
            raise SelectionError(f"unknown event type: {kind}")
    statuses = {s["status"] for s in slots.values()}
    paused = exhausted or bool(statuses & {"intent", "invalid", "conflict"})
    return {
        "config": config,
        "slots": slots,
        "paused": paused,
        "waiting": bool(statuses & {"pending"}),
        "complete": statuses == {"accepted"},
        "exhausted": exhausted,
        "excluded_seeds": sorted(excluded),
    }


class Ledger:
    """One immutable ledger for one authorized selection, with serialized writes."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        _require(self.path.is_file(), "ledger missing; never silently recreate it")
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def create(cls, path: Path, config: dict, audit_path: Path) -> "Ledger":
        """Create exactly once; caller verifies audit and approval before init."""
        config = {**config, "audit_document": audit_path.read_bytes().decode("utf-8")}
        _configuration(config)
        path = Path(path).resolve()
        with path.open("xb"):
            pass
        ledger = cls(path)
        with ledger._connect() as conn:
            conn.execute(
                "CREATE TABLE events(seq INTEGER PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL)"
            )
            for operation in ("UPDATE", "DELETE"):
                conn.execute(
                    f"CREATE TRIGGER no_{operation.lower()} BEFORE {operation} ON events "
                    "BEGIN SELECT RAISE(ABORT, 'append only'); END"
                )
            conn.execute(
                "CREATE TRIGGER no_replace BEFORE INSERT ON events "
                "WHEN EXISTS(SELECT 1 FROM events WHERE seq=NEW.seq) "
                "BEGIN SELECT RAISE(ABORT, 'append only'); END"
            )
            ledger._append(conn, {"event_type": "init", "config": config})
        return ledger

    @staticmethod
    def _append(conn: sqlite3.Connection, event: dict) -> None:
        last = conn.execute(
            "SELECT seq,digest FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq, previous = (last[0] + 1, last[1]) if last else (1, ZERO_HASH)
        record = {
            **event,
            "event_seq": seq,
            "utc": datetime.now(timezone.utc).isoformat(),
            "previous_hash": previous,
        }
        body = _json(record)
        digest = hashlib.sha256(body.encode()).hexdigest()
        conn.execute("INSERT INTO events VALUES(?,?,?)", (seq, body, digest))

    @staticmethod
    def _read(conn: sqlite3.Connection) -> tuple[dict, list[dict], str]:
        rows = conn.execute(
            "SELECT seq,body,digest FROM events ORDER BY seq"
        ).fetchall()
        _require(bool(rows), "empty/unfinished ledger")
        previous = ZERO_HASH
        events = []
        for expected, (seq, body, digest) in enumerate(rows, 1):
            event = json.loads(body)
            _require(isinstance(event, dict), "invalid event record")
            _require(seq == expected == event.get("event_seq"), "event sequence gap")
            _require(
                hashlib.sha256(body.encode()).hexdigest() == digest,
                "event hash mismatch",
            )
            _require(event.get("previous_hash") == previous, "broken event chain")
            _require(isinstance(event.get("utc"), str), "missing event time")
            previous = digest
            events.append(event)
        _require(events[0].get("event_type") == "init", "missing initialization")
        return events[0]["config"], events[1:], previous

    def status(self) -> dict:
        """Return replayed state and tail anchor; no random calls or data generation."""
        with self._connect() as conn:
            config, events, tail = self._read(conn)
            return {
                **_state(config, events),
                "tail_hash": tail,
                "event_count": len(events) + 1,
                "events": events,
            }

    def draw(
        self,
        source: Path,
        audit_path: Path,
        *,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> dict:
        """Persist intent before RNG; any exception leaves an unresolved intent."""
        with self._connect() as conn:
            config, events, _ = self._read(conn)
            audit_sha256 = hashlib.sha256(audit_path.read_bytes()).hexdigest()
            _require(
                audit_sha256 == config["audit_sha256"],
                "source/audit version mismatch",
            )
            _verify_source(source, config["source_commit"])
            state = _state(config, events)
            _require(
                not state["paused"] and not state["waiting"] and not state["complete"],
                "selection paused, waiting, or complete",
            )
            slot_id = min(
                int(k) for k, s in state["slots"].items() if s["status"] == "empty"
            )
            event = {
                "event_type": "draw_intent",
                "slot_id": slot_id,
                "attempt_no": state["slots"][str(slot_id)]["used"] + 1,
                "seed": None,
                "method_blob": config["method_blob"],
            }
            _state(config, events + [event])
            self._append(conn, event)
        raw = randbelow(MAX_SEED)
        _require(type(raw) is int, "invalid RNG return; intent remains unresolved")
        returned = {**event, "event_type": "draw_returned", "seed": raw + 1}
        with self._connect() as conn:
            config, events, _ = self._read(conn)
            _state(config, events + [returned])
            self._append(conn, returned)
        # Invalid returned values remain paused until an explicit recorded decision.
        return self.status()

    def record(
        self,
        event_type: str,
        slot_id: int,
        attempt_no: int,
        seed: int,
        receipt: str,
        **details: object,
    ) -> dict:
        """Append an operator-verified personal declaration or A decision receipt."""
        _require(
            event_type in {"declare", "accept", "reject", "revoke"},
            "use draw for RNG events",
        )
        _receipt(receipt)
        with self._connect() as conn:
            config, events, _ = self._read(conn)
            event = {
                **details,
                "event_type": event_type,
                "slot_id": slot_id,
                "attempt_no": attempt_no,
                "seed": seed,
                "receipt": receipt,
                "method_blob": config["method_blob"],
            }
            _state(config, events + [event])
            self._append(conn, event)
        return self.status()


def main(argv: list[str] | None = None) -> int:
    """Command line adapter; successful selection never implies frozen rules."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--config", required=True, type=Path)
    initialize.add_argument("--audit", required=True, type=Path)
    commands.add_parser("status")
    draw = commands.add_parser("draw")
    draw.add_argument("--source", required=True, type=Path)
    draw.add_argument("--audit", required=True, type=Path)
    for action in ("declare", "accept", "reject", "revoke"):
        command = commands.add_parser(action)
        command.add_argument("--slot", required=True, type=int)
        command.add_argument("--attempt", required=True, type=int)
        command.add_argument("--seed", required=True, type=int)
        command.add_argument("--receipt", required=True)
        if action == "declare":
            command.add_argument("--actor", required=True)
            command.add_argument("--contacted", choices=("yes", "no"), required=True)
        if action in ("reject", "revoke"):
            command.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        ledger = Ledger(args.ledger.resolve())
        if args.command == "init":
            ledger = Ledger.create(
                args.ledger,
                json.loads(args.config.read_text(encoding="utf-8")),
                args.audit,
            )
        elif args.command == "draw":
            ledger.draw(args.source, args.audit)
        elif args.command != "status":
            details = {}
            if args.command == "declare":
                details = {"actor": args.actor, "contacted": args.contacted == "yes"}
            elif args.command in ("reject", "revoke"):
                details = {"reason": args.reason}
            ledger.record(
                args.command,
                args.slot,
                args.attempt,
                args.seed,
                args.receipt,
                **details,
            )
        print(json.dumps(ledger.status(), indent=2))
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        subprocess.CalledProcessError,
    ) as exc:
        print(
            json.dumps({"error": str(exc), "action": "stop_and_audit; do not redraw"})
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
