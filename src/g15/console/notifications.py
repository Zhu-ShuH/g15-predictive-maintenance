"""Local notification control: authorize source reads before durable delivery."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import threading
from pathlib import Path
from typing import Callable, Any

from g15.notifications import (
    AlertSnapshot,
    NotificationPolicy,
    NotificationService,
    compose_message,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class NotificationRequestError(Exception):
    """A fixed public error, never provider exception text or credentials."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


class NotificationController:
    """Poll committed per-device snapshots; delivery never runs on analysis threads."""

    def __init__(
        self,
        application: Any,
        snapshot_provider: Callable,
        state_path: Path,
        *,
        service: NotificationService | None = None,
        load_environment_token: bool = True,
    ):
        if not callable(snapshot_provider):
            raise ValueError("notification source must be callable")
        self.application, self.snapshot_provider = application, snapshot_provider
        self.service = (
            service if service is not None else NotificationService(state_path)
        )
        token = os.environ.get("G15_PUSHPLUS_TOKEN") if load_environment_token else None
        if token:
            current = self.service.status()["policy"]
            self.service.configure(NotificationPolicy(**current), token=token)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_checked_at: str | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        """Start independent source polling and the bounded delivery dispatcher."""
        if self._thread is not None:
            raise RuntimeError("notification monitor already started")
        self.service.start()
        self._thread = threading.Thread(
            target=self._run, name="g15-notifications", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """Stop source reads before closing the independent delivery worker."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=8)
            if self._thread.is_alive():
                raise RuntimeError("notification monitor did not stop")
        self.service.close()

    def _snapshot(self, pair: tuple[str, str]) -> AlertSnapshot:
        if pair not in self.application.allowed:
            raise NotificationRequestError(404, "not_found")
        status, payload = self.application.upstream_get(pair, {"page_size": "1"})
        if status != 200:
            raise NotificationRequestError(status, "notification_source_unavailable")
        body = json.loads(payload)
        if body.get("run_id") != pair[0] or body.get("device_id") != pair[1]:
            raise NotificationRequestError(502, "invalid_upstream_response")
        try:
            snapshot = self.snapshot_provider(pair)
            if (
                not isinstance(snapshot, AlertSnapshot)
                or (snapshot.run_id, snapshot.device_id) != pair
                or not snapshot.complete
            ):
                raise ValueError("invalid notification snapshot")
            return snapshot
        except Exception as exc:
            raise NotificationRequestError(
                502, "notification_snapshot_unavailable"
            ) from exc

    def preview(self, pair: tuple[str, str]) -> dict:
        """Build the exact delivery body without adding a message or sending it."""
        snapshot = self._snapshot(pair)
        policy = self.service.status()["policy"]
        message = compose_message(snapshot, policy["red_threshold"])
        return {
            **message,
            "red_count": snapshot.red_count,
            "advice": list(snapshot.advice),
            "report_generated_at": snapshot.report_json.get("generated_at_utc"),
            "triggered": snapshot.red_count >= policy["red_threshold"],
            "run_id": snapshot.run_id,
            "device_id": snapshot.device_id,
        }

    def configure(self, payload: dict) -> dict:
        """Apply a strict policy; credentials are never returned or persisted here."""
        allowed = {"enabled", "threshold", "cooldown_seconds", "token", "channel"}
        if set(payload) - allowed or not {
            "enabled",
            "threshold",
            "cooldown_seconds",
        } <= set(payload):
            raise NotificationRequestError(400, "invalid_request")
        if payload.get("channel", "pushplus") != "pushplus":
            raise NotificationRequestError(400, "invalid_request")
        current = self.service.status()
        token = payload.get("token")
        if token is not None and not isinstance(token, str):
            raise NotificationRequestError(400, "invalid_request")
        if payload["enabled"] is True and not (
            token or (current["configured"] and token is None)
        ):
            raise NotificationRequestError(400, "not_configured")
        try:
            policy = NotificationPolicy(
                enabled=payload["enabled"],
                red_threshold=payload["threshold"],
                cooldown_seconds=payload["cooldown_seconds"],
            )
            self.service.configure(policy, token=token)
        except ValueError as exc:
            raise NotificationRequestError(400, "invalid_request") from exc
        return self.status()

    def test(self, pair: tuple[str, str]) -> dict:
        """Queue an explicitly requested test with the actual report and advice."""
        if not self.service.status()["configured"]:
            raise NotificationRequestError(400, "not_configured")
        try:
            result = self.service.enqueue_test(self._snapshot(pair))
        except NotificationRequestError:
            raise
        except ValueError as exc:
            raise NotificationRequestError(400, "invalid_request") from exc
        if result.get("decision") != "queued":
            status, code = {
                "outbox_full": (503, "notification_outbox_full"),
                "content_too_large": (413, "notification_content_too_large"),
                "token_missing": (400, "not_configured"),
                "incomplete_snapshot": (502, "notification_snapshot_unavailable"),
            }.get(result.get("decision"), (503, "notification_enqueue_failed"))
            raise NotificationRequestError(status, code)
        return {
            "queued": True,
            "notification": {"id": result["id"], "decision": "queued"},
        }

    def status(self) -> dict:
        """Expose only a fixed, bounded projection of settings and delivery history."""
        source = self.service.status()
        policy = source["policy"]
        with self._lock:
            checked, error = self._last_checked_at, self._last_error
        error = error or source.get("last_error")
        latest = source.get("latest", [])
        if not error and latest and latest[0].get("state") in ("failed", "retry_wait"):
            error = latest[0].get("error_code")
        idle = False
        if source["configured"] and policy["enabled"]:
            try:
                live = self.application.live_status()
                idle = live["status"] != "running" or not any(
                    row.get("last_commit_at_utc") for row in live["devices"]
                )
            except Exception:
                error = "notification_source_unavailable"
        state = (
            "unconfigured"
            if not source["configured"]
            else (
                "paused"
                if not policy["enabled"]
                else "error" if error else "source_idle" if idle else "monitoring"
            )
        )

        def timestamp(value):
            if isinstance(value, str):
                return value
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                return (
                    datetime.fromtimestamp(value, timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            return None

        history = []
        for row in latest[:50]:
            public = {
                key: row.get(key)
                for key in (
                    "id",
                    "kind",
                    "run_id",
                    "device_id",
                    "red_count",
                    "threshold",
                    "state",
                    "attempts",
                    "error_code",
                )
            }
            for name in ("created_at", "next_attempt_at", "accepted_at"):
                public[name] = timestamp(
                    row.get(
                        name,
                        (
                            row.get("updated_at")
                            if name == "accepted_at"
                            and row.get("state") == "provider_accepted"
                            else None
                        ),
                    )
                )
            public["uncertain"] = row.get("uncertain") is True
            history.append(public)
        return {
            "settings": {
                "enabled": policy["enabled"],
                "threshold": policy["red_threshold"],
                "cooldown_seconds": policy["cooldown_seconds"],
                "channel": "pushplus",
            },
            "configured": source["configured"],
            "status": {
                "state": state,
                "last_checked_at": checked,
                "last_error_code": error,
            },
            "history": history,
            "delivery_scope": "provider_accepted_not_handset_delivery",
        }

    def scan_once(self) -> None:
        """Observe each eligible device separately; do not claim a fleet transaction."""
        source = self.service.status()
        if not source["configured"] or not source["policy"]["enabled"]:
            return
        try:
            live = self.application.live_status()
            if live["status"] not in (
                "running",
                "stopping",
                "stopped",
                "limit_reached",
            ):
                raise ValueError("notification source failed")
            now = datetime.now(timezone.utc)
            devices = {
                (row["run_id"], row["device_id"]): row for row in live["devices"]
            }
            unavailable = False
            for pair in sorted(self.application.allowed):
                if self._stop.is_set():
                    return
                row = devices.get(pair)
                if row is None or row.get("error"):
                    unavailable = True
                    continue
                if not row.get("last_commit_at_utc"):
                    continue
                stamp = datetime.fromisoformat(
                    row["last_commit_at_utc"].replace("Z", "+00:00")
                )
                age = (now - stamp).total_seconds()
                if age < 0 or (
                    live["status"] in ("running", "stopping")
                    and age > max(10, live["interval_seconds"] * 5)
                ):
                    unavailable = True
                    continue
                decision = self.service.observe(self._snapshot(pair))
                if decision.get("error_code") or decision.get("decision") in (
                    "stale_snapshot",
                    "inconsistent_snapshot",
                    "incomplete_snapshot",
                ):
                    unavailable = True
            with self._lock:
                self._last_checked_at = _utc_now()
                self._last_error = (
                    "notification_source_unavailable" if unavailable else None
                )
        except Exception:
            with self._lock:
                self._last_checked_at = _utc_now()
                self._last_error = "notification_source_unavailable"

    def _run(self) -> None:
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(2)


class EmailNotificationController(NotificationController):
    """Independent 163 mail settings, previews and explicit tests over one snapshot."""

    def __init__(self, application, snapshot_provider, state_path, *, service=None):
        from g15.notifications.smtp import EmailNotificationService

        super().__init__(
            application,
            snapshot_provider,
            state_path,
            service=(
                service if service is not None else EmailNotificationService(state_path)
            ),
            load_environment_token=False,
        )

    def preview(self, pair: tuple[str, str]) -> dict:
        """Preview complete advice/report text and exact UTF-8 attachment sizes."""
        snapshot = self._snapshot(pair)
        policy = self.service.status()["policy"]
        message = compose_message(snapshot, policy["red_threshold"])
        report_json = json.dumps(
            dict(snapshot.report_json),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        return {
            **message,
            "red_count": snapshot.red_count,
            "advice": list(snapshot.advice),
            "report_generated_at": snapshot.report_json.get("generated_at_utc"),
            "triggered": snapshot.red_count >= policy["red_threshold"],
            "run_id": snapshot.run_id,
            "device_id": snapshot.device_id,
            "attachments": [
                {
                    "filename": "summary.md",
                    "content_type": "text/markdown; charset=utf-8",
                    "bytes": len(snapshot.report_markdown.encode("utf-8")),
                },
                {
                    "filename": "summary.json",
                    "content_type": "application/json; charset=utf-8",
                    "bytes": len(report_json.encode("utf-8")),
                },
            ],
        }

    def configure(self, payload: dict) -> dict:
        """Save route and policy atomically; a local authorization code stays in memory."""
        required = {"enabled", "threshold", "cooldown_seconds", "sender", "recipient"}
        if (
            not isinstance(payload, dict)
            or set(payload) - (required | {"auth_code", "channel"})
            or not required <= set(payload)
            or payload.get("channel", "smtp163") != "smtp163"
            or ("auth_code" in payload and not isinstance(payload["auth_code"], str))
        ):
            raise NotificationRequestError(400, "invalid_request")
        try:
            policy = NotificationPolicy(
                enabled=payload["enabled"],
                red_threshold=payload["threshold"],
                cooldown_seconds=payload["cooldown_seconds"],
            )
            self.service.configure_email(
                policy,
                sender=payload["sender"],
                recipient=payload["recipient"],
                auth_code=payload.get("auth_code"),
            )
        except ValueError as exc:
            code = (
                "not_configured" if str(exc) == "not_configured" else "invalid_request"
            )
            raise NotificationRequestError(400, code) from None
        return self.status()

    def status(self) -> dict:
        """Report SMTP acceptance only and no authorization code or credential handle."""
        result = super().status()
        result["settings"].update(self.service.email_settings())
        result["delivery_scope"] = "smtp_accepted_not_handset_delivery"
        return result
