"""Durable source cursors and bounded asynchronous mobile-alert outbox."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable
import uuid

from .models import AlertSnapshot, MAX_CONTENT_BYTES, NotificationPolicy, require_time
from .transport import ProviderError, ProviderReceipt, PushPlusTransport, validate_token


class NotificationControlError(ValueError):
    """A fixed conflict code for a safe notification control operation."""


class NotificationStorageError(Exception):
    """Safe storage failure code, with no local path or SQL error details."""


def compose_message(
    snapshot: AlertSnapshot, threshold: int, *, test: bool = False
) -> dict:
    """Build the exact preview/send body, preserving both full public reports."""
    NotificationPolicy(red_threshold=threshold)
    if type(test) is not bool:
        raise ValueError("invalid notification test flag")
    body = "> 测试通知：用于验证手机渠道；不代表产生新的红色告警。\n\n" if test else ""
    body += f"配置阈值：每设备本运行累计红色事件 ≥ {threshold} 条。\n\n"
    body += snapshot.content()
    if len(body.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("notification_content_too_large")
    return {
        "title": ("G15 测试通知 · " if test else "G15 红色告警 · ")
        + snapshot.device_id[:75],
        "markdown": body,
    }


class NotificationService:
    """Observe committed snapshots, atomically enqueue, dispatch outside collection.

    Provider acceptance is the highest success state. A crash after remote
    acceptance but before local commit can repeat the message on lease recovery;
    this is explicitly at-least-once delivery attempts, not exactly-once delivery.
    Token values exist only in this process and are never stored in SQLite.
    """

    def __init__(
        self,
        db_path: Path | str,
        policy: NotificationPolicy | None = None,
        *,
        transport=None,
        clock: Callable[[], float] | None = None,
        message_builder: Callable = compose_message,
        coalesce_deferred: bool = True,
        provider_name: str = "pushplus_wechat",
    ):
        if not callable(message_builder) or type(coalesce_deferred) is not bool:
            raise ValueError("invalid notification message builder")
        self._message_builder = message_builder
        self._coalesce_deferred = coalesce_deferred
        self._provider_name = provider_name
        self._path = Path(db_path)
        self._lock = threading.RLock()
        self._dispatch_lock = threading.Lock()
        self._clock = clock or time.time
        self._transport = transport if transport is not None else PushPlusTransport()
        self._token: str | None = None
        self._environment_token_suppressed = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._background_error: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS notification_settings (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1), policy_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS notification_preferences (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        environment_token_suppressed INTEGER NOT NULL
                            CHECK(environment_token_suppressed IN (0,1))
                    );
                    CREATE TABLE IF NOT EXISTS notification_sources (
                        run_id TEXT NOT NULL, device_id TEXT NOT NULL,
                        observed_count INTEGER NOT NULL, observed_red_id TEXT,
                        enqueued_count INTEGER NOT NULL DEFAULT 0,
                        enqueued_red_id TEXT, last_enqueued_at REAL,
                        PRIMARY KEY(run_id,device_id)
                    );
                    CREATE TABLE IF NOT EXISTS notification_outbox (
                        id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE NOT NULL,
                        kind TEXT NOT NULL, run_id TEXT NOT NULL, device_id TEXT NOT NULL,
                        red_count INTEGER NOT NULL, last_red_id TEXT, threshold INTEGER,
                        title TEXT NOT NULL, markdown TEXT NOT NULL,
                        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL, retry_seconds INTEGER NOT NULL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL,
                        next_attempt_at REAL NOT NULL, lease_until REAL,
                        deferred INTEGER NOT NULL DEFAULT 0,
                        provider_reference TEXT, error_code TEXT,
                        uncertain INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS notification_due
                    ON notification_outbox(state,next_attempt_at);
                """)
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(notification_outbox)"
                    )
                }
                if "threshold" not in columns:
                    connection.execute(
                        "ALTER TABLE notification_outbox ADD COLUMN threshold INTEGER"
                    )
                if "deferred" not in columns:
                    connection.execute(
                        "ALTER TABLE notification_outbox ADD COLUMN deferred INTEGER NOT NULL DEFAULT 0"
                    )
                connection.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS notification_deferred_pair
                    ON notification_outbox(run_id,device_id)
                    WHERE deferred=1 AND kind='automatic' AND state='queued' AND attempts=0"""
                )
                row = connection.execute(
                    "SELECT policy_json FROM notification_settings WHERE singleton=1"
                ).fetchone()
                self._policy = (
                    policy
                    if policy is not None
                    else (
                        NotificationPolicy(**json.loads(row[0]))
                        if row
                        else NotificationPolicy()
                    )
                )
                preference = connection.execute(
                    "SELECT environment_token_suppressed FROM notification_preferences WHERE singleton=1"
                ).fetchone()
                self._environment_token_suppressed = bool(preference and preference[0])
                if not isinstance(self._policy, NotificationPolicy):
                    raise ValueError("invalid notification policy")
                connection.execute(
                    "INSERT INTO notification_settings VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET policy_json=excluded.policy_json",
                    (json.dumps(self._policy.public()),),
                )
        except (OSError, sqlite3.Error, ValueError, TypeError):
            raise NotificationStorageError("notification_storage_error") from None

    def _connect(self):
        connection = sqlite3.connect(str(self._path), timeout=1.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @property
    def policy(self) -> NotificationPolicy:
        """Return the immutable non-secret current policy."""
        with self._lock:
            return self._policy

    @property
    def environment_token_suppressed(self) -> bool:
        """Whether deletion forbids reloading a deployment-provided token on restart."""
        with self._lock:
            return self._environment_token_suppressed

    def configure(self, policy: NotificationPolicy, token: str | None = None) -> dict:
        """Persist policy; None keeps token and empty string clears it in memory."""
        if not isinstance(policy, NotificationPolicy):
            raise ValueError("invalid notification policy")
        if token is not None and token != "":
            validate_token(token)
        with self._lock:
            try:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE notification_settings SET policy_json=? WHERE singleton=1",
                        (json.dumps(policy.public()),),
                    )
                    if token:
                        connection.execute(
                            "INSERT INTO notification_preferences VALUES(1,0) ON CONFLICT(singleton) DO UPDATE SET environment_token_suppressed=0"
                        )
                    connection.commit()
            except sqlite3.Error:
                raise NotificationStorageError("notification_storage_error") from None
            self._policy = policy
            if token is not None:
                self._token = token or None
            if token:
                self._environment_token_suppressed = False
        self._wake.set()
        return self.status()

    def _check_control_idle(self, connection) -> None:
        """Reject ambiguous work rather than cancelling a possible delivery."""
        if (
            getattr(self._transport, "request_inflight", False)
            or connection.execute(
                """SELECT 1 FROM notification_outbox WHERE state IN ('sending','retry_wait')
            OR (state='queued' AND attempts>0) LIMIT 1"""
            ).fetchone()
        ):
            raise NotificationControlError("notification_busy")

    @staticmethod
    def _cancel_unattempted(connection, now, *, automatic_only=False) -> int:
        condition = " AND kind='automatic'" if automatic_only else ""
        cursor = connection.execute(
            """UPDATE notification_outbox SET state='failed',
            error_code='notification_cancelled',uncertain=0,lease_until=NULL,
            deferred=0,updated_at=? WHERE state='queued' AND attempts=0""" + condition,
            (now,),
        )
        return cursor.rowcount

    def _delete_channel_settings(self, connection) -> None:
        """Clear optional channel-specific persisted settings in the same transaction."""
        del connection

    def _forget_channel_credentials(self) -> None:
        """Forget process-only credentials after a successful settings transaction."""
        self._token = None

    def delete_settings(self) -> dict:
        """Disable/reset this channel and cancel only never-attempted queued work."""
        if not self._dispatch_lock.acquire(blocking=False):
            raise NotificationControlError("notification_busy")
        try:
            with self._lock:
                policy = NotificationPolicy()
                now = require_time(self._clock())
                try:
                    with closing(self._connect()) as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        self._check_control_idle(connection)
                        cancelled = self._cancel_unattempted(connection, now)
                        connection.execute(
                            "UPDATE notification_settings SET policy_json=? WHERE singleton=1",
                            (json.dumps(policy.public()),),
                        )
                        self._delete_channel_settings(connection)
                        connection.execute(
                            "INSERT INTO notification_preferences VALUES(1,1) ON CONFLICT(singleton) DO UPDATE SET environment_token_suppressed=1"
                        )
                        connection.commit()
                except sqlite3.Error:
                    raise NotificationStorageError(
                        "notification_storage_error"
                    ) from None
                self._policy = policy
                self._environment_token_suppressed = True
                self._forget_channel_credentials()
                self._background_error = None
        finally:
            self._dispatch_lock.release()
        self._wake.set()
        return {"deleted": True, "cancelled_count": cancelled}

    def cancel_pending_automatic(self) -> dict:
        """Cancel paused, never-attempted automatic jobs while preserving all evidence."""
        if not self._dispatch_lock.acquire(blocking=False):
            raise NotificationControlError("notification_busy")
        try:
            with self._lock:
                if self._policy.enabled:
                    raise NotificationControlError("notification_enabled")
                now = require_time(self._clock())
                try:
                    with closing(self._connect()) as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        self._check_control_idle(connection)
                        cancelled = self._cancel_unattempted(
                            connection, now, automatic_only=True
                        )
                        connection.commit()
                except sqlite3.Error:
                    raise NotificationStorageError(
                        "notification_storage_error"
                    ) from None
        finally:
            self._dispatch_lock.release()
        return {"cancelled_count": cancelled}

    def observe(self, snapshot: AlertSnapshot) -> dict:
        """Freeze new red events now; defer/coalesce delivery until the cooldown slot."""
        if not isinstance(snapshot, AlertSnapshot):
            raise ValueError("invalid alert snapshot")
        with self._lock:
            if not snapshot.complete:
                return {"decision": "incomplete_snapshot"}
            if not self._policy.enabled:
                return {"decision": "disabled"}
            if not self._token:
                return {"decision": "token_missing"}
            if snapshot.red_count < self._policy.red_threshold:
                return {"decision": "below_threshold"}
            try:
                message = self._message_builder(snapshot, self._policy.red_threshold)
            except ValueError:
                return {
                    "decision": "content_too_large",
                    "error_code": "notification_content_too_large",
                }
            now = require_time(self._clock())
            try:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    source = connection.execute(
                        "SELECT * FROM notification_sources WHERE run_id=? AND device_id=?",
                        (snapshot.run_id, snapshot.device_id),
                    ).fetchone()
                    if source and snapshot.red_count < source["observed_count"]:
                        connection.rollback()
                        return {"decision": "stale_snapshot"}
                    if (
                        source
                        and snapshot.red_count == source["observed_count"]
                        and snapshot.last_red_id != source["observed_red_id"]
                    ):
                        connection.rollback()
                        return {"decision": "inconsistent_snapshot"}
                    connection.execute(
                        """INSERT INTO notification_sources(run_id,device_id,observed_count,observed_red_id)
                        VALUES(?,?,?,?) ON CONFLICT(run_id,device_id) DO UPDATE SET observed_count=excluded.observed_count,observed_red_id=excluded.observed_red_id""",
                        (
                            snapshot.run_id,
                            snapshot.device_id,
                            snapshot.red_count,
                            snapshot.last_red_id,
                        ),
                    )
                    if source and (
                        snapshot.red_count <= source["enqueued_count"]
                        or snapshot.last_red_id == source["enqueued_red_id"]
                    ):
                        connection.commit()
                        return {"decision": "already_queued"}
                    result, scheduled_at = self._enqueue_automatic(
                        connection, snapshot, message, now, source
                    )
                    if result["decision"] in ("queued", "coalesced"):
                        connection.execute(
                            "UPDATE notification_sources SET enqueued_count=?,enqueued_red_id=?,last_enqueued_at=? WHERE run_id=? AND device_id=?",
                            (
                                snapshot.red_count,
                                snapshot.last_red_id,
                                scheduled_at,
                                snapshot.run_id,
                                snapshot.device_id,
                            ),
                        )
                    connection.commit()
            except sqlite3.Error:
                raise NotificationStorageError("notification_storage_error") from None
        self._wake.set()
        return result

    def _enqueue_automatic(self, connection, snapshot, message, now, source):
        """Freeze cooldown work durably, merging only a never-attempted delayed job."""
        pending = connection.execute(
            """SELECT id,next_attempt_at FROM notification_outbox
            WHERE run_id=? AND device_id=? AND kind='automatic' AND deferred=1
            AND state='queued' AND attempts=0 ORDER BY created_at LIMIT 1""",
            (snapshot.run_id, snapshot.device_id),
        ).fetchone()
        if pending is not None and self._coalesce_deferred:
            # The source cursor and replacement content commit together. A dispatcher
            # claims under BEGIN IMMEDIATE, so sending/uncertain/retry jobs are immutable.
            connection.execute(
                """UPDATE notification_outbox
                SET dedupe_key=?,red_count=?,last_red_id=?,threshold=?,title=?,markdown=?,
                updated_at=?,max_attempts=?,retry_seconds=? WHERE id=?
                AND state='queued' AND attempts=0""",
                (
                    self._dedupe(snapshot),
                    snapshot.red_count,
                    snapshot.last_red_id,
                    self._policy.red_threshold,
                    message["title"],
                    message["markdown"],
                    now,
                    self._policy.max_attempts,
                    self._policy.retry_seconds,
                    pending["id"],
                ),
            )
            return {
                "decision": "coalesced",
                "id": pending["id"],
                "eligible_at": pending["next_attempt_at"],
            }, pending["next_attempt_at"]
        eligible_at = now
        if source and source["last_enqueued_at"] is not None:
            eligible_at = max(
                now, source["last_enqueued_at"] + self._policy.cooldown_seconds
            )
        result = self._insert(
            connection, snapshot, message, now, test=False, scheduled_at=eligible_at
        )
        if result["decision"] == "queued" and eligible_at > now:
            result["eligible_at"] = eligible_at
        return result, eligible_at

    @staticmethod
    def _dedupe(snapshot):
        return hashlib.sha256(
            json.dumps(
                [snapshot.run_id, snapshot.device_id, snapshot.last_red_id],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def enqueue_test(self, snapshot: AlertSnapshot) -> dict:
        """Explicit user test; bypass auto enable/threshold without consuming its cursor."""
        if not isinstance(snapshot, AlertSnapshot):
            raise ValueError("invalid alert snapshot")
        with self._lock:
            if not self._token:
                return {"decision": "token_missing"}
            if not snapshot.complete:
                return {"decision": "incomplete_snapshot"}
            try:
                message = self._message_builder(
                    snapshot, self._policy.red_threshold, test=True
                )
            except ValueError:
                return {
                    "decision": "content_too_large",
                    "error_code": "notification_content_too_large",
                }
            try:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    result = self._insert(
                        connection,
                        snapshot,
                        message,
                        require_time(self._clock()),
                        test=True,
                    )
                    connection.commit()
            except sqlite3.Error:
                raise NotificationStorageError("notification_storage_error") from None
        self._wake.set()
        return result

    def _insert(self, connection, snapshot, message, now, *, test, scheduled_at=None):
        if (
            connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
            >= 10000
        ):
            return {"decision": "outbox_full", "error_code": "notification_outbox_full"}
        identifier = uuid.uuid4().hex
        dedupe = "test:" + identifier if test else self._dedupe(snapshot)
        scheduled_at = now if scheduled_at is None else scheduled_at
        cursor = connection.execute(
            """INSERT OR IGNORE INTO notification_outbox
            (id,dedupe_key,kind,run_id,device_id,red_count,last_red_id,threshold,title,markdown,state,max_attempts,retry_seconds,created_at,updated_at,next_attempt_at,deferred)
            VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,?)""",
            (
                identifier,
                dedupe,
                "test" if test else "automatic",
                snapshot.run_id,
                snapshot.device_id,
                snapshot.red_count,
                snapshot.last_red_id,
                self._policy.red_threshold,
                message["title"],
                message["markdown"],
                self._policy.max_attempts,
                self._policy.retry_seconds,
                now,
                now,
                scheduled_at,
                int(self._coalesce_deferred and not test and scheduled_at > now),
            ),
        )
        return (
            {"decision": "queued", "id": identifier}
            if cursor.rowcount
            else {"decision": "already_queued"}
        )

    def dispatch_once(self) -> dict:
        """Attempt at most one due item, with a durable lease and bounded retry count."""
        if not self._dispatch_lock.acquire(blocking=False):
            return {"decision": "busy"}
        try:
            return self._dispatch_one()
        finally:
            self._dispatch_lock.release()

    def _dispatch_one(self):
        with self._lock:
            if not self._token:
                return {"decision": "token_missing"}
            now = require_time(self._clock())
            try:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    # A disabled policy pauses automatic jobs but still permits explicit tests.
                    job = connection.execute(
                        """SELECT * FROM notification_outbox WHERE
                        (kind='test' OR ?=1) AND
                        ((state IN ('queued','retry_wait') AND next_attempt_at<=?)
                        OR (state='sending' AND lease_until<=?))
                        ORDER BY created_at,id LIMIT 1""",
                        (int(self._policy.enabled), now, now),
                    ).fetchone()
                    if job is None:
                        connection.commit()
                        return {"decision": "idle"}
                    if job["attempts"] >= job["max_attempts"]:
                        connection.execute(
                            "UPDATE notification_outbox SET state='failed',error_code='delivery_attempt_limit',uncertain=1,lease_until=NULL,updated_at=? WHERE id=?",
                            (now, job["id"]),
                        )
                        connection.commit()
                        return {"decision": "failed", "id": job["id"]}
                    connection.execute(
                        "UPDATE notification_outbox SET state='sending',attempts=attempts+1,lease_until=?,updated_at=?,uncertain=1 WHERE id=?",
                        (now + 10, now, job["id"]),
                    )
                    connection.commit()
                    token = self._token
            except sqlite3.Error:
                raise NotificationStorageError("notification_storage_error") from None
        receipt = None
        failure = None
        try:
            receipt = self._transport.send(token, job["title"], job["markdown"])
            if not isinstance(receipt, ProviderReceipt):
                raise ProviderError("provider_receipt_invalid", uncertain=True)
        except ProviderError as exc:
            failure = exc
        except Exception:
            failure = ProviderError("provider_internal_error", uncertain=True)
        now = require_time(self._clock())
        attempt = job["attempts"] + 1
        with self._lock:
            try:
                with closing(self._connect()) as connection:
                    if failure is None:
                        reference = receipt.reference
                        if reference and token in reference:
                            reference = None
                        state, code, uncertain = "provider_accepted", None, 0
                    else:
                        state = (
                            "retry_wait"
                            if failure.retryable and attempt < job["max_attempts"]
                            else "failed"
                        )
                        reference, code, uncertain = (
                            None,
                            failure.code,
                            int(failure.uncertain),
                        )
                    delay = min(3600, job["retry_seconds"] * 2 ** max(0, attempt - 1))
                    connection.execute(
                        """UPDATE notification_outbox SET state=?,provider_reference=?,error_code=?,uncertain=?,next_attempt_at=?,lease_until=NULL,updated_at=? WHERE id=?""",
                        (
                            state,
                            reference,
                            code,
                            uncertain,
                            now + delay,
                            now,
                            job["id"],
                        ),
                    )
            except sqlite3.Error:
                raise NotificationStorageError("notification_storage_error") from None
        return {
            "decision": state,
            "id": job["id"],
            "error_code": code,
            "uncertain": bool(uncertain),
        }

    def status(self) -> dict:
        """Return bounded safe metadata; never include content, token or exception text."""
        with self._lock:
            try:
                with closing(self._connect()) as connection:
                    counts = {
                        row[0]: row[1]
                        for row in connection.execute(
                            "SELECT state,COUNT(*) FROM notification_outbox GROUP BY state"
                        )
                    }
                    rows = connection.execute(
                        """SELECT id,kind,run_id,device_id,red_count,threshold,state,attempts,created_at,updated_at,next_attempt_at,provider_reference,error_code,uncertain
                        FROM notification_outbox ORDER BY created_at DESC,rowid DESC LIMIT 20"""
                    ).fetchall()
            except sqlite3.Error:
                raise NotificationStorageError("notification_storage_error") from None
            latest = [dict(row) for row in rows]
            for row in latest:
                row["uncertain"] = bool(row["uncertain"])
            return {
                "policy": self._policy.public(),
                "configured": bool(self._token),
                "provider": self._provider_name,
                "delivery_semantics": "provider_acceptance_only",
                "counts": counts,
                "latest": latest,
                "last_error": self._background_error,
            }

    def start(self) -> None:
        """Start a single independent bounded dispatcher thread."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="g15-mobile-notifications", daemon=True
            )
            self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                result = self.dispatch_once()
                self._background_error = None
                if result["decision"] not in ("idle", "token_missing", "busy"):
                    continue
            except Exception:
                self._background_error = "notification_dispatch_error"
            self._wake.wait(0.5)
            self._wake.clear()

    def close(self) -> None:
        """Stop dispatch after the bounded in-flight request completes."""
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(6)
            if self._thread.is_alive():
                self._background_error = "notification_shutdown_incomplete"
                raise RuntimeError("notification_shutdown_incomplete")
        if getattr(self._transport, "request_inflight", False):
            self._background_error = "notification_request_unfinished"
            raise RuntimeError("notification_request_unfinished")
