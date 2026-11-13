"""Fixed-origin HTTPS PushPlus adapter with bounded bodies and safe errors.

Official contract: https://github.com/pushplus/pushplus/blob/main/guide/api.md
A code=200 response means provider acceptance only, never handset delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import re
import socket
import ssl
import time
import threading

from .models import MAX_CONTENT_BYTES

MAX_RESPONSE_BYTES = 64 * 1024
_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_ERRORS = frozenset(
    {
        "provider_timeout",
        "provider_network_error",
        "provider_http_error",
        "provider_redirect_rejected",
        "provider_response_too_large",
        "provider_invalid_response",
        "provider_rejected",
        "provider_payload_too_large",
        "provider_internal_error",
        "provider_receipt_invalid",
        "provider_request_in_progress",
    }
)


class ProviderError(Exception):
    """A fixed public code; never retain provider messages, token or exception text."""

    def __init__(self, code: str, *, retryable: bool = True, uncertain: bool = False):
        self.code = code if code in _SAFE_ERRORS else "provider_internal_error"
        self.retryable = bool(retryable)
        self.uncertain = bool(uncertain)
        super().__init__(self.code)


@dataclass(frozen=True)
class ProviderReceipt:
    """Provider acceptance acknowledgement, with an optional bounded opaque id."""

    reference: str | None = None

    def __post_init__(self):
        if self.reference is not None and (
            not isinstance(self.reference, str)
            or not _SAFE_REFERENCE.fullmatch(self.reference)
        ):
            raise ValueError("invalid provider reference")


def validate_token(token: str) -> str:
    """Accept a bounded single-line ASCII token without logging its value."""
    if (
        not isinstance(token, str)
        or not 8 <= len(token) <= 256
        or not all(33 <= ord(char) <= 126 for char in token)
    ):
        raise ValueError("invalid notification token")
    return token


class PushPlusTransport:
    """POST to the fixed TLS origin; redirects are never followed."""

    def __init__(self, timeout_seconds: float = 5.0):
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 5
        ):
            raise ValueError("invalid notification transport timeout")
        self.timeout_seconds = float(timeout_seconds)
        self._guard = threading.Lock()
        self._inflight: threading.Thread | None = None

    def send(self, token: str, title: str, content: str) -> ProviderReceipt:
        """Return provider acceptance only; all unsafe exceptions become fixed codes."""
        validate_token(token)
        if (
            not isinstance(title, str)
            or not 1 <= len(title) <= 100
            or not isinstance(content, str)
        ):
            raise ProviderError("provider_payload_too_large", retryable=False)
        body = json.dumps(
            {
                "token": token,
                "title": title,
                "content": content,
                "template": "markdown",
                "channel": "wechat",
            },
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(body) > MAX_CONTENT_BYTES:
            raise ProviderError("provider_payload_too_large", retryable=False)
        deadline = time.monotonic() + self.timeout_seconds
        result = {}
        done = threading.Event()

        def request_worker():
            try:
                result["receipt"] = self._send_request(token, body, deadline)
            except ProviderError as exc:
                result["error"] = exc
            except Exception:
                result["error"] = ProviderError(
                    "provider_internal_error", uncertain=True
                )
            finally:
                done.set()

        with self._guard:
            if self.request_inflight:
                raise ProviderError("provider_request_in_progress", uncertain=True)
            self._inflight = threading.Thread(
                target=request_worker, name="g15-pushplus-request", daemon=True
            )
            self._inflight.start()
        if (
            not done.wait(max(0, deadline - time.monotonic()))
            or time.monotonic() > deadline
        ):
            raise ProviderError("provider_timeout", uncertain=True)
        self._inflight.join(max(0, deadline - time.monotonic()))
        if "error" in result:
            raise result["error"]
        return result["receipt"]

    @property
    def request_inflight(self) -> bool:
        """Expose unfinished DNS/TLS work so shutdown cannot claim full closure.

        OS DNS can outlive a socket timeout. At most one daemon request worker
        can exist per transport; subsequent attempts do not spawn more workers.
        """
        return self._inflight is not None and self._inflight.is_alive()

    def _send_request(self, token, body, deadline):
        connection = http.client.HTTPSConnection(
            "www.pushplus.plus",
            timeout=self.timeout_seconds,
            context=ssl.create_default_context(),
        )
        try:
            # Resolve/connect before POST; a late DNS result cannot send a stale body.
            connection.connect()
            self._remaining(connection.sock, deadline)
            connection.request(
                "POST",
                "/send",
                body=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                },
            )
            self._remaining(connection.sock, deadline)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ProviderError("provider_redirect_rejected", retryable=False)
            if response.status != 200:
                raise ProviderError(
                    "provider_http_error",
                    retryable=response.status == 429 or response.status >= 500,
                )
            data = bytearray()
            while True:
                # HTTPResponse owns the socket for Connection: close responses.
                raw_socket = connection.sock or getattr(
                    getattr(response.fp, "raw", None), "_sock", None
                )
                self._remaining(raw_socket, deadline)
                chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ProviderError("provider_response_too_large", uncertain=True)
            try:
                payload = json.loads(bytes(data).decode("utf-8"))
            except (ValueError, UnicodeError):
                raise ProviderError(
                    "provider_invalid_response", uncertain=True
                ) from None
            if not isinstance(payload, dict) or type(payload.get("code")) is not int:
                raise ProviderError("provider_invalid_response", uncertain=True)
            if payload["code"] != 200:
                raise ProviderError(
                    "provider_rejected", retryable=payload["code"] in (500, 600, 900)
                )
            reference = payload.get("data")
            if isinstance(reference, dict):
                reference = reference.get("shortcode", reference.get("shortCode"))
            if (
                not isinstance(reference, str)
                or not _SAFE_REFERENCE.fullmatch(reference)
                or token in reference
            ):
                reference = None
            return ProviderReceipt(reference)
        except ProviderError:
            raise
        except (TimeoutError, socket.timeout):
            raise ProviderError("provider_timeout", uncertain=True) from None
        except (OSError, http.client.HTTPException):
            raise ProviderError("provider_network_error", uncertain=True) from None
        except Exception:
            raise ProviderError("provider_internal_error", uncertain=True) from None
        finally:
            connection.close()

    @staticmethod
    def _remaining(sock, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        if sock is not None:
            sock.settimeout(remaining)
