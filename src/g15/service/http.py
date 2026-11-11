"""Minimal HTTP/1.1 transport for the authenticated health query."""

from __future__ import annotations

import io
import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

from g15.query.application import (
    DEFAULT_PAGE_SIZE,
    QUERY_SHAPE,
    AuthenticationError,
    HealthQueryApplication,
    InvalidQueryError,
    QueryError,
    QueryResponse,
    ResourceNotFoundError,
)

EventSink = Callable[[Mapping[str, object]], None]
_LOGGER = logging.getLogger(__name__)
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_WORKERS = 16
MAX_REQUEST_PATH_BYTES = 16_384
MAX_QUERY_BYTES = 16_384
MAX_OVERLOAD_HEADER_BYTES = 16_384
MAX_OVERLOAD_HEADER_SECONDS = 0.1


class _DeadlineSocketReader(io.RawIOBase):
    """Raw socket reader enforcing one absolute monotonic deadline."""

    def __init__(
        self,
        connection: socket.socket,
        *,
        deadline: float,
        idle_timeout_seconds: float,
    ) -> None:
        super().__init__()
        self._connection = connection
        self._deadline = deadline
        self._idle_timeout_seconds = idle_timeout_seconds

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request header deadline exceeded")
        self._connection.settimeout(min(self._idle_timeout_seconds, remaining))
        try:
            return self._connection.recv_into(buffer)
        except socket.timeout as exc:
            raise TimeoutError("request header deadline exceeded") from exc


class QueryHTTPServer(ThreadingHTTPServer):
    """Bounded, short-connection transport for the query application."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(
        self,
        server_address: tuple[str, int],
        application: HealthQueryApplication,
        *,
        event_sink: EventSink | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> None:
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or not 0 < float(request_timeout_seconds) <= 60
        ):
            raise ValueError("request_timeout_seconds must be in (0, 60]")
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or not 1 <= max_workers <= 256
        ):
            raise ValueError("max_workers must be in [1, 256]")
        self.application = application
        self.event_sink = event_sink or _log_event
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.max_workers = max_workers
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(server_address, QueryRequestHandler)

    def get_request(  # pylint: disable=consider-using-with
        self,
    ) -> tuple[socket.socket, tuple[str, int]]:
        """Accept one connection and bound all blocking socket operations."""
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout_seconds)
        return request, client_address

    def process_request(  # pylint: disable=consider-using-with
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        """Start at most ``max_workers`` request threads."""
        if not self._worker_slots.acquire(blocking=False):
            self._reject_overloaded(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()

    def _reject_overloaded(self, request: socket.socket) -> None:
        request_id = _request_id()
        request_head_complete = self._read_overload_request_head(request)
        body = _json_payload(
            {
                "error": "internal",
                "request_id": request_id,
                "trace_id": request_id,
            }
        )
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Cache-Control: no-store\r\n"
            b"X-Content-Type-Options: nosniff\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        if request_head_complete:
            try:
                request.settimeout(
                    min(self.request_timeout_seconds, MAX_OVERLOAD_HEADER_SECONDS)
                )
                request.sendall(response)
                request.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        try:
            self.event_sink(
                {
                    "timestamp_utc": _utc_now(),
                    "request_id": request_id,
                    "trace_id": request_id,
                    "stage": "rejected",
                    "query_shape": QUERY_SHAPE,
                    "result": "overloaded",
                }
            )
        except Exception:  # pylint: disable=broad-exception-caught
            _LOGGER.error("query transport event sink failed")

    def _read_overload_request_head(self, request: socket.socket) -> bool:
        """Drain one complete small request head before a graceful 503 close."""
        deadline = time.monotonic() + min(
            self.request_timeout_seconds,
            MAX_OVERLOAD_HEADER_SECONDS,
        )
        received = bytearray()
        while b"\r\n\r\n" not in received:
            remaining = deadline - time.monotonic()
            available = MAX_OVERLOAD_HEADER_BYTES - len(received)
            if remaining <= 0 or available <= 0:
                return False
            request.settimeout(remaining)
            try:
                chunk = request.recv(min(4_096, available))
            except (OSError, TimeoutError):
                return False
            if not chunk:
                return False
            received.extend(chunk)
        return True


class QueryRequestHandler(BaseHTTPRequestHandler):
    """Map one frozen GET resource onto the read-only application query."""

    protocol_version = "HTTP/1.1"
    server_version = "G15Query/1"
    sys_version = ""

    def setup(self) -> None:
        """Install a reader whose header deadline cannot be reset by trickle."""
        super().setup()
        self.rfile.close()
        raw_reader = _DeadlineSocketReader(
            self.connection,
            deadline=time.monotonic() + self._server.request_timeout_seconds,
            idle_timeout_seconds=self._server.request_timeout_seconds,
        )
        self.rfile = io.BufferedReader(raw_reader)

    def do_GET(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Serve one authenticated, authorized, read-only health query."""
        self.connection.settimeout(self._server.request_timeout_seconds)
        self.close_connection = True
        request_id = _request_id()
        self._emit(request_id, "server_received", "received")
        response_context: QueryResponse | None = None
        try:
            self._validate_request_framing()
            headers = self.headers.get_all("Authorization", failobj=[])
            if len(headers) != 1:
                raise AuthenticationError("authentication required")
            principal = self._server.application.authenticate(
                authorization=headers[0],
                request_id=request_id,
            )
            run_id, device_id, parameters = _parse_target(self.path)
            authorized = self._server.application.authorize(
                principal=principal,
                run_id=run_id,
                device_id=device_id,
                request_id=request_id,
            )
            page_size, windows_cursor, alarms_cursor = _parse_parameters(parameters)
            response_context = self._server.application.query_authorized(
                authorized=authorized,
                request_id=request_id,
                page_size=page_size,
                windows_cursor=windows_cursor,
                alarms_cursor=alarms_cursor,
            )
            body = dict(response_context.body)
            body["request_id"] = request_id
            body["trace_id"] = request_id
            result = (
                "success"
                if self._send_json(HTTPStatus.OK, body)
                else "connection_error"
            )
        except AuthenticationError:
            self._send_error(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                request_id,
                authenticate=True,
            )
            result = "unauthorized"
        except ResourceNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", request_id)
            result = "not_found"
        except InvalidQueryError:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", request_id)
            result = "invalid_request"
        except QueryError:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", request_id)
            result = "internal"
        except OSError:
            result = "connection_error"
        except Exception:  # pylint: disable=broad-exception-caught
            # Transport parsing and socket-adjacent failures share one body;
            # exception text can contain paths or request data and is never sent.
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", request_id)
            result = "internal"
        if result != "success":
            self._emit(request_id, "rejected", result, response_context)
        self._emit(request_id, "response_completed", result, response_context)

    def __getattr__(self, name: str) -> Any:
        """Route every syntactically valid non-GET method to one JSON 405."""
        if name.startswith("do_") and len(name) > 3:
            return self._method_not_allowed
        raise AttributeError(name)

    def handle_expect_100(self) -> bool:
        """Reject Expect before the base class can emit an interim 100 response."""
        self.connection.settimeout(self._server.request_timeout_seconds)
        self.close_connection = True
        request_id = _request_id()
        self._emit(request_id, "server_received", "received")
        self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", request_id)
        self._emit(request_id, "rejected", "invalid_request")
        self._emit(request_id, "response_completed", "invalid_request")
        return False

    def log_message(  # pylint: disable=arguments-differ
        self, _format: str, *args: Any
    ) -> None:
        """Suppress the base logger because it records the raw resource path."""
        del args

    @property
    def _server(self) -> QueryHTTPServer:
        return cast(QueryHTTPServer, self.server)

    def _method_not_allowed(self) -> None:
        self.connection.settimeout(self._server.request_timeout_seconds)
        self.close_connection = True
        request_id = _request_id()
        self._emit(request_id, "server_received", "received")
        try:
            self._validate_request_framing()
        except InvalidQueryError:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", request_id)
            result = "invalid_request"
        else:
            self._send_error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "method_not_allowed",
                request_id,
                extra_headers={"Allow": "GET"},
            )
            result = "method_not_allowed"
        self._emit(request_id, "rejected", result)
        self._emit(request_id, "response_completed", result)

    def _validate_request_framing(self) -> None:
        """Allow only an empty, unambiguous request body on this read API."""
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            raise InvalidQueryError("request body is not allowed")
        if self.headers.get_all("Expect", failobj=[]):
            raise InvalidQueryError("request body is not allowed")
        content_lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(content_lengths) > 1:
            raise InvalidQueryError("request body is not allowed")
        if not content_lengths:
            return
        raw_length = content_lengths[0].strip()
        if (
            not raw_length
            or len(raw_length) > 10
            or not raw_length.isascii()
            or not raw_length.isdecimal()
            or int(raw_length) != 0
        ):
            raise InvalidQueryError("request body is not allowed")

    def _send_error(
        self,
        status: HTTPStatus,
        code: str,
        request_id: str,
        *,
        authenticate: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> bool:
        headers = dict(extra_headers or {})
        if authenticate:
            headers["WWW-Authenticate"] = "Bearer"
        return self._send_json(
            status,
            {"error": code, "request_id": request_id, "trace_id": request_id},
            extra_headers=headers or None,
        )

    def _send_json(
        self,
        status: HTTPStatus,
        body: Mapping[str, object],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> bool:
        payload = _json_payload(body)
        self.close_connection = True
        try:
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            if extra_headers is not None:
                for name, value in extra_headers.items():
                    self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except OSError:
            return False
        return True

    def _emit(
        self,
        request_id: str,
        stage: str,
        result: str,
        response: QueryResponse | None = None,
    ) -> None:
        event: dict[str, object] = {
            "timestamp_utc": _utc_now(),
            "request_id": request_id,
            "trace_id": request_id,
            "stage": stage,
            "query_shape": QUERY_SHAPE,
            "result": result,
        }
        if response is not None:
            event["actor_ref"] = response.actor_ref
            event["permission_domain"] = response.permission_domain
            event["manifest_schema_version"] = response.manifest_schema_version
        try:
            self._server.event_sink(event)
        except Exception:  # pylint: disable=broad-exception-caught
            _LOGGER.error("query transport event sink failed")


def create_query_server(  # pylint: disable=too-many-arguments
    application: HealthQueryApplication,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    event_sink: EventSink | None = None,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> QueryHTTPServer:
    """Create, but do not start, the thin HTTP server.

    The default binds to loopback. Deployments binding elsewhere must provide
    reviewed TLS termination; this module neither provisions nor claims TLS.
    """
    return QueryHTTPServer(
        (host, port),
        application,
        event_sink=event_sink,
        request_timeout_seconds=request_timeout_seconds,
        max_workers=max_workers,
    )


def _parse_target(target: str) -> tuple[str, str, str]:
    try:
        parsed = urlsplit(target)
    except ValueError as exc:
        raise InvalidQueryError("invalid request target") from exc
    if (
        len(parsed.path.encode("utf-8", errors="surrogatepass"))
        > MAX_REQUEST_PATH_BYTES
    ):
        raise InvalidQueryError("invalid request target")
    parts = parsed.path.split("/")
    if len(parts) != 7 or parts[:3] != ["", "v1", "runs"]:
        raise ResourceNotFoundError("resource not found")
    if parts[4] != "devices" or parts[6] != "health":
        raise ResourceNotFoundError("resource not found")
    run_id = unquote(parts[3])
    device_id = unquote(parts[5])
    if not run_id or not device_id:
        raise ResourceNotFoundError("resource not found")
    return run_id, device_id, parsed.query


def _parse_parameters(query: str) -> tuple[int, str | None, str | None]:
    if len(query.encode("utf-8", errors="surrogatepass")) > MAX_QUERY_BYTES:
        raise InvalidQueryError("invalid query parameters")
    try:
        parameters = parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=3,
        )
    except ValueError as exc:
        raise InvalidQueryError("invalid query parameters") from exc
    allowed = {"page_size", "windows_cursor", "alarms_cursor"}
    has_duplicate = any(len(values) != 1 for values in parameters.values())
    if set(parameters) - allowed or has_duplicate:
        raise InvalidQueryError("invalid query parameters")
    raw_page_size = parameters.get("page_size", [str(DEFAULT_PAGE_SIZE)])[0]
    if (
        len(raw_page_size) > 3
        or not raw_page_size.isascii()
        or not raw_page_size.isdecimal()
    ):
        raise InvalidQueryError("invalid page size")
    windows_cursor = _optional_nonempty(parameters, "windows_cursor")
    alarms_cursor = _optional_nonempty(parameters, "alarms_cursor")
    return int(raw_page_size), windows_cursor, alarms_cursor


def _optional_nonempty(parameters: Mapping[str, list[str]], name: str) -> str | None:
    if name not in parameters:
        return None
    value = parameters[name][0]
    if not value:
        raise InvalidQueryError("invalid cursor")
    return value


def _request_id() -> str:
    prefix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}-{secrets.token_urlsafe(12)}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_payload(body: Mapping[str, object]) -> bytes:
    return json.dumps(
        body,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _log_event(event: Mapping[str, object]) -> None:
    """Emit one already-redacted transport event through standard logging."""
    _LOGGER.info("%s", _json_payload(event).decode("ascii"))
