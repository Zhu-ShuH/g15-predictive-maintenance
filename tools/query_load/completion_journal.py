"""Bounded off-loop persistence of already completed HTTP observations."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from queue import Full, Queue
import threading
from typing import Any

from .accounting import ClientRecord


class CompletionJournalError(RuntimeError):
    """Evidence failed; known records remain available and the wave must fail."""


class CompletionJournal:
    """One writer, at most one immutable record/mapping pair per planned call.

    The selector has already decoded and recorded each terminal time before
    emit. No producer waits for a write/flush. Joining happens after network
    work and never changes client timestamps or received/completed intervals.
    """

    def __init__(self, evidence: Any, limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid completion journal limit")
        self.evidence = evidence
        self.limit = limit
        self._queue: Queue = Queue(maxsize=limit)
        self._stop = object()
        self._items: list[tuple[ClientRecord, dict]] = []
        self._written: list[int] = []
        self._errors: list[str] = []
        self._max_pending = 0
        self._closed = False
        self._drained = False
        self._fallback: dict | None = None
        self._thread = threading.Thread(target=self._write, daemon=True)
        self._thread.start()

    def emit(self, record: ClientRecord, mapping: dict) -> None:
        """Freeze one terminal pair without disk I/O or a blocking put."""
        if self._closed or len(self._items) >= self.limit:
            raise CompletionJournalError("completion journal capacity/state violation")
        item = deepcopy(record), deepcopy(mapping)
        sequence = len(self._items)
        self._items.append(item)
        try:
            self._queue.put_nowait((sequence, item))
        except Full as exc:
            self._errors.append("queue_full")
            raise CompletionJournalError("completion journal queue full") from exc
        self._max_pending = max(self._max_pending, self._queue.qsize())

    def _write(self) -> None:
        while True:
            value = self._queue.get()
            try:
                if value is self._stop:
                    return
                sequence, (record, mapping) = value
                if self._errors:
                    # Never retry a possibly partial raw write. Every submitted
                    # pair is still retained for the separately hashed fallback.
                    continue
                kind = (
                    "client"
                    if mapping.get("preparation", {}).get("http_started") is True
                    else "client_attempt"
                )
                self.evidence.write(kind, asdict(record))
                self.evidence.write("request_connection", mapping)
                self.evidence.rows["request_connection"].append(mapping)
                self._written.append(sequence)
            except BaseException as exc:  # pylint: disable=broad-exception-caught
                self._errors.append(type(exc).__name__)
            finally:
                self._queue.task_done()

    def _save_failure(self) -> None:
        """Keep all known pairs, including any uncertain partial raw writes."""
        value = {
            "format": "g15-completion-journal-failure-v1",
            "quality_passed": False,
            "errors": list(self._errors),
            "fully_written_pair_sequences": list(self._written),
            "partial_raw_write_may_exist": True,
            "normal_raw_one_copy_claim_applies": False,
            "items": [
                {"sequence": index, "record": asdict(record), "mapping": mapping}
                for index, (record, mapping) in enumerate(self._items)
            ],
        }
        payload = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
        path = Path(self.evidence.directory) / "completion-journal-failure.json"
        try:
            with path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._fallback = {
                "path": path.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "pairs": len(self._items),
            }
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            self._errors.append("fallback_" + type(exc).__name__)

    def finish(self) -> None:
        """Drain once after HTTP processing, retaining failure without retry."""
        if self._closed:
            if self._errors:
                raise CompletionJournalError("completion evidence failed")
            return
        self._closed = True
        try:
            self._queue.put(self._stop, timeout=5)
        except Full:
            self._errors.append("stop_queue_timeout")
        self._thread.join(timeout=10)
        self._drained = (
            not self._thread.is_alive() and self._queue.unfinished_tasks == 0
        )
        if not self._drained:
            self._errors.append("writer_drain_timeout")
        if self._errors:
            self.evidence.errors.extend("completion_journal_" + e for e in self._errors)
            self._save_failure()
            raise CompletionJournalError("completion evidence failed")

    def facts(self) -> dict:
        """Return safe counts; an error always prevents an accepted wave."""
        return {
            "mode": "bounded single writer; terminal timestamps precede enqueue",
            "capacity_pairs": self.limit,
            "submitted_pairs": len(self._items),
            "fully_written_pairs": len(self._written),
            "max_pending_pairs_observed": self._max_pending,
            "drained": self._drained,
            "errors": list(self._errors),
            "fallback": self._fallback,
        }
