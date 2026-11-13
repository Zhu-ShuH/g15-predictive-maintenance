"""Fixed 163 SSL SMTP delivery with frozen envelopes and memory-only credentials."""

from __future__ import annotations

from contextlib import closing
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
import json
from pathlib import Path
import re
import secrets
import smtplib
import socket
import ssl
import threading
import time
import uuid

from .models import AlertSnapshot, MAX_CONTENT_BYTES, NotificationPolicy
from .service import NotificationService, NotificationStorageError, compose_message
from .transport import ProviderError, ProviderReceipt, validate_token

SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465
MAX_WIRE_BYTES = 256 * 1024
_EMAIL_LOCAL = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z"
)
_EMAIL_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def validate_email_address(value: str, *, sender: bool = False) -> str:
    """Accept exactly one bounded ASCII mailbox, never a list or header syntax."""
    if (
        not isinstance(value, str)
        or not 3 <= len(value) <= 254
        or not all(33 <= ord(char) <= 126 for char in value)
        or value.count("@") != 1
    ):
        raise ValueError("invalid email address")
    local, domain = value.rsplit("@", 1)
    labels = domain.split(".")
    if (
        len(local) > 64
        or not _EMAIL_LOCAL.fullmatch(local)
        or len(domain) > 253
        or len(labels) < 2
        or not all(_EMAIL_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
        or (sender and domain.lower() != "163.com")
    ):
        raise ValueError("invalid email address")
    return local + "@" + domain.lower()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def compose_email_message(
    snapshot, threshold, *, sender, recipient, test=False
) -> dict:
    """Freeze routes and both original reports from one snapshot, without secrets."""
    message = compose_message(snapshot, threshold, test=test)
    envelope = {
        "schema_version": "1",
        "message_id": f"<{uuid.uuid4().hex}@g15.local>",
        "sender": validate_email_address(sender, sender=True),
        "recipient": validate_email_address(recipient),
        "body": message["markdown"],
        "report_markdown": snapshot.report_markdown,
        "report_json": _json(dict(snapshot.report_json)),
    }
    content = _json(envelope)
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("notification_content_too_large")
    return {"title": message["title"], "markdown": content}


def _decode_envelope(content):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate SMTP field")
            result[key] = value
        return result

    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("invalid SMTP payload")
    envelope = json.loads(
        content,
        object_pairs_hook=unique,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )
    if (
        not isinstance(envelope, dict)
        or set(envelope)
        != {
            "schema_version",
            "message_id",
            "sender",
            "recipient",
            "body",
            "report_markdown",
            "report_json",
        }
        or envelope["schema_version"] != "1"
        or not isinstance(envelope["message_id"], str)
        or not re.fullmatch(r"<[a-f0-9]{32}@g15\.local>", envelope["message_id"])
        or any(
            not isinstance(envelope[key], str) or not envelope[key]
            for key in ("body", "report_markdown", "report_json")
        )
        or envelope["sender"] != validate_email_address(envelope["sender"], sender=True)
        or envelope["recipient"] != validate_email_address(envelope["recipient"])
    ):
        raise ValueError("invalid SMTP payload")
    report = json.loads(
        envelope["report_json"],
        object_pairs_hook=unique,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )
    if not isinstance(report, dict) or _json(report) != envelope["report_json"]:
        raise ValueError("invalid SMTP report")
    return envelope


def _wire_message(title, envelope):
    if (
        not isinstance(title, str)
        or not 1 <= len(title) <= 100
        or any(ord(char) < 32 or ord(char) == 127 for char in title)
    ):
        raise ValueError("invalid SMTP title")
    message = EmailMessage(policy=SMTP)
    message["Subject"] = title
    message["From"] = envelope["sender"]
    message["To"] = envelope["recipient"]
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = envelope["message_id"]
    message.set_content(envelope["body"], charset="utf-8")
    message.add_attachment(
        envelope["report_markdown"].encode("utf-8"),
        maintype="text",
        subtype="markdown",
        filename="summary.md",
    )
    message.add_attachment(
        envelope["report_json"].encode("utf-8"),
        maintype="application",
        subtype="json",
        filename="summary.json",
    )
    wire = message.as_bytes()
    if len(wire) > MAX_WIRE_BYTES:
        raise ValueError("smtp_payload_too_large")
    return wire


class _DeadlineSMTP(smtplib.SMTP_SSL):
    """SMTP over verified TLS with one absolute deadline, including DNS late returns."""

    def __init__(self, *, deadline, cancelled, register_socket, data_started):
        self._deadline = deadline
        self._cancelled = cancelled
        self._register_socket = register_socket
        self._data_started = data_started
        super().__init__(
            SMTP_HOST,
            SMTP_PORT,
            timeout=self._remaining(),
            context=ssl.create_default_context(),
        )

    def _remaining(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0 or self._cancelled.is_set():
            raise TimeoutError()
        return remaining

    def _get_socket(self, host, port, timeout):
        del timeout
        if host != SMTP_HOST or port != SMTP_PORT:
            raise ValueError("invalid SMTP origin")
        addresses = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        self._remaining()  # DNS cannot trigger a late connect or stale delivery.
        last_error = None
        for family, kind, protocol, _name, address in addresses[:8]:
            raw = socket.socket(family, kind, protocol)
            self._register_socket(raw)
            try:
                raw.settimeout(self._remaining())
                raw.connect(address)
                raw.settimeout(self._remaining())
                secure = self.context.wrap_socket(raw, server_hostname=SMTP_HOST)
                self._register_socket(secure)
                secure.settimeout(self._remaining())
                return secure
            except ssl.SSLError:
                raw.close()
                raise
            except OSError as exc:
                last_error = exc
                raw.close()
                self._remaining()
        raise last_error or OSError("SMTP unavailable")

    def send(self, value):
        self.sock.settimeout(self._remaining())
        return super().send(value)

    def getreply(self):
        if self.file is None:
            self.file = self.sock.makefile("rb")
        lines, size = [], 0
        while True:
            self.sock.settimeout(self._remaining())
            line = self.file.readline(8193)
            if not line:
                raise smtplib.SMTPServerDisconnected("SMTP reply unavailable")
            size += len(line)
            if len(line) > 8192 or size > 65536:
                raise smtplib.SMTPResponseException(500, b"invalid SMTP reply")
            if len(line) < 4 or not line[:3].isdigit() or line[3:4] not in (b"-", b" "):
                raise smtplib.SMTPResponseException(500, b"invalid SMTP reply")
            code = int(line[:3])
            lines.append(line[4:].strip(b" \t\r\n"))
            if line[3:4] != b"-":
                self._remaining()
                return code, b"\n".join(lines)

    def data(self, message):
        self.putcmd("data")
        code, response = self.getreply()
        if code != 354:
            raise smtplib.SMTPDataError(code, response)
        if isinstance(message, str):
            message = message.encode("ascii")
        message = re.sub(rb"(?m)^\.", b"..", message)
        if not message.endswith(b"\r\n"):
            message += b"\r\n"
        self._data_started.set()
        self.send(message + b".\r\n")
        return self.getreply()


class SMTP163Transport:
    """Only smtp.163.com:465; provider acceptance never means phone reception."""

    def __init__(self, timeout_seconds: float = 5.0, *, smtp_factory=None):
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 5
            or (smtp_factory is not None and not callable(smtp_factory))
        ):
            raise ValueError("invalid SMTP transport timeout")
        self.timeout_seconds = float(timeout_seconds)
        self._smtp_factory = smtp_factory or _DeadlineSMTP
        self._guard = threading.RLock()
        self._credential = None
        self._inflight = None
        self._active_socket = None

    def register_credentials(self, handle: str, sender: str, auth_code: str) -> None:
        """Replace the sole memory-only credential; queued envelopes remain frozen."""
        validate_token(handle)
        sender = validate_email_address(sender, sender=True)
        validate_token(auth_code)
        with self._guard:
            self._credential = (handle, sender, auth_code)

    def clear_credentials(self) -> None:
        """Forget credentials without changing any already queued envelope."""
        with self._guard:
            self._credential = None

    @property
    def request_inflight(self) -> bool:
        """Whether the single daemon SMTP operation still exists."""
        return self._inflight is not None and self._inflight.is_alive()

    def _register_socket(self, sock):
        with self._guard:
            self._active_socket = sock

    def _abort_socket(self):
        with self._guard:
            sock = self._active_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def send(self, handle: str, title: str, content: str) -> ProviderReceipt:
        """Attempt one immutable message with bounded work and safe uncertainty."""
        deadline = time.monotonic() + self.timeout_seconds
        try:
            if (
                isinstance(content, str)
                and len(content.encode("utf-8")) > MAX_CONTENT_BYTES
            ):
                raise ProviderError("smtp_payload_too_large", retryable=False)
            envelope = _decode_envelope(content)
            wire = _wire_message(title, envelope)
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            code = (
                "smtp_payload_too_large"
                if str(exc) == "smtp_payload_too_large"
                else "smtp_payload_invalid"
            )
            raise ProviderError(code, retryable=False) from None
        with self._guard:
            if self.request_inflight:
                raise ProviderError("provider_request_in_progress", uncertain=True)
            credentials = self._credential
            if not credentials or credentials[:2] != (handle, envelope["sender"]):
                raise ProviderError("smtp_sender_credentials_missing", retryable=False)
            self._active_socket = None
            cancelled, data_started, done = (
                threading.Event(),
                threading.Event(),
                threading.Event(),
            )
            result = {}

            def worker():
                try:
                    result["receipt"] = self._send_request(
                        credentials,
                        envelope,
                        wire,
                        deadline,
                        cancelled,
                        data_started,
                        lambda: result.update(receipt=ProviderReceipt()),
                    )
                except ProviderError as exc:
                    result["error"] = exc
                except Exception:
                    result["error"] = ProviderError(
                        "provider_internal_error", uncertain=data_started.is_set()
                    )
                finally:
                    done.set()

            self._inflight = threading.Thread(
                target=worker, name="g15-smtp163-request", daemon=True
            )
            self._inflight.start()
        if (
            not done.wait(max(0, deadline - time.monotonic()))
            or time.monotonic() > deadline
        ):
            cancelled.set()
            self._abort_socket()
            if "receipt" in result:
                return result["receipt"]
            raise ProviderError("smtp_timeout", uncertain=data_started.is_set())
        self._inflight.join(max(0, deadline - time.monotonic()))
        if "receipt" in result:
            return result["receipt"]
        raise result["error"]

    def _send_request(
        self, credentials, envelope, wire, deadline, cancelled, data_started, accepted
    ):
        client = None

        def check():
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise TimeoutError()

        try:
            check()
            client = self._smtp_factory(
                deadline=deadline,
                cancelled=cancelled,
                register_socket=self._register_socket,
                data_started=data_started,
            )
            check()
            code, _reply = client.ehlo()
            if code != 250:
                raise ProviderError("smtp_protocol_error", retryable=400 <= code < 500)
            check()
            code, _reply = client.login(credentials[1], credentials[2])
            if code not in (235, 503):
                raise ProviderError("smtp_auth_failed", retryable=False)
            check()
            refused = client.sendmail(envelope["sender"], [envelope["recipient"]], wire)
            if refused:
                raise ProviderError("smtp_recipient_rejected", retryable=False)
            accepted()  # Preserve the observed DATA 250 even if socket cleanup stalls.
            return ProviderReceipt()
        except ProviderError:
            raise
        except smtplib.SMTPAuthenticationError:
            raise ProviderError("smtp_auth_failed", retryable=False) from None
        except smtplib.SMTPSenderRefused as exc:
            raise ProviderError(
                "smtp_sender_rejected", retryable=400 <= exc.smtp_code < 500
            ) from None
        except smtplib.SMTPRecipientsRefused as exc:
            retryable = bool(exc.recipients) and all(
                400 <= value[0] < 500 for value in exc.recipients.values()
            )
            raise ProviderError(
                "smtp_recipient_rejected", retryable=retryable
            ) from None
        except smtplib.SMTPDataError as exc:
            raise ProviderError(
                "smtp_data_rejected", retryable=400 <= exc.smtp_code < 500
            ) from None
        except ssl.SSLError:
            raise ProviderError("smtp_tls_error", retryable=False) from None
        except (TimeoutError, socket.timeout):
            raise ProviderError(
                "smtp_timeout", uncertain=data_started.is_set()
            ) from None
        except (OSError, smtplib.SMTPServerDisconnected):
            raise ProviderError(
                "smtp_network_error", uncertain=data_started.is_set()
            ) from None
        except smtplib.SMTPException:
            raise ProviderError(
                "smtp_protocol_error", uncertain=data_started.is_set()
            ) from None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            self._abort_socket()


class EmailNotificationService(NotificationService):
    """Independent durable SMTP queue; credentials never enter its database."""

    def __init__(self, db_path: Path | str, policy=None, *, transport=None, clock=None):
        self._sender, self._recipient, self._auth_code = "", "", None
        smtp = transport if transport is not None else SMTP163Transport()
        super().__init__(
            db_path,
            policy,
            transport=smtp,
            clock=clock,
            message_builder=self._compose,
            coalesce_deferred=False,
            provider_name="smtp163",
        )
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS email_settings (singleton INTEGER PRIMARY KEY CHECK(singleton=1),sender TEXT NOT NULL,recipient TEXT NOT NULL)"
                )
                row = connection.execute(
                    "SELECT sender,recipient FROM email_settings WHERE singleton=1"
                ).fetchone()
                if row:
                    self._sender = validate_email_address(row["sender"], sender=True)
                    self._recipient = validate_email_address(row["recipient"])
        except Exception:
            raise NotificationStorageError("notification_storage_error") from None

    def _compose(self, snapshot, threshold, *, test=False):
        return compose_email_message(
            snapshot,
            threshold,
            sender=self._sender,
            recipient=self._recipient,
            test=test,
        )

    def email_settings(self) -> dict:
        """Return only the non-secret current route."""
        with self._lock:
            return {
                "sender": self._sender,
                "recipient": self._recipient,
                "channel": "smtp163",
            }

    def configure_email(self, policy, *, sender, recipient, auth_code=None) -> dict:
        """Commit route and policy together, then atomically replace memory credentials."""
        if not isinstance(policy, NotificationPolicy):
            raise ValueError("invalid notification policy")
        sender = validate_email_address(sender, sender=True)
        recipient = validate_email_address(recipient)
        if auth_code is not None and auth_code != "":
            validate_token(auth_code)
        with self._lock:
            if self._sender and sender != self._sender and auth_code is None:
                raise ValueError("not_configured")
            candidate = (
                self._auth_code
                if auth_code is None and sender == self._sender
                else auth_code or None
            )
            if policy.enabled and not candidate:
                raise ValueError("not_configured")
            handle = (
                self._token
                if candidate and sender == self._sender and self._token
                else secrets.token_hex(32) if candidate else None
            )
            try:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE notification_settings SET policy_json=? WHERE singleton=1",
                        (json.dumps(policy.public()),),
                    )
                    connection.execute(
                        "INSERT INTO email_settings VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET sender=excluded.sender,recipient=excluded.recipient",
                        (sender, recipient),
                    )
                    connection.commit()
            except Exception:
                raise NotificationStorageError("notification_storage_error") from None
            if handle:
                self._transport.register_credentials(handle, sender, candidate)
            else:
                self._transport.clear_credentials()
            self._sender, self._recipient, self._auth_code = (
                sender,
                recipient,
                candidate,
            )
            self._policy, self._token = policy, handle
        self._wake.set()
        return self.status()

    def close(self) -> None:
        """Stop workers and forget memory credentials; unresolved work still raises."""
        try:
            super().close()
        finally:
            with self._lock:
                self._transport.clear_credentials()
                self._auth_code, self._token = None, None
