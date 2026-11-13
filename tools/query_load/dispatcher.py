"""Fair, direct HTTP/1 dispatch over explicitly metered nonblocking sockets.

Only concurrent D0 waves use this adapter. TCP and header preparation precede
control acknowledgement; each actual send/read/decode has its own timestamps.
No response is held to manufacture overlap. Framing deliberately supports the
controlled endpoint's single Content-Length responses, not arbitrary HTTP.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from http.client import HTTPConnection
import ipaddress
import json
import math
import re
import selectors
import time
from typing import Any
from urllib.parse import quote, urlsplit

from .accounting import ClientRecord, RunAccounting
from .client import _finite_float, _invalid_constant
from .network import MeteredHTTPConnection, SocketNetworkMeter, is_would_block

MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 16 * 1024 * 1024
_READ_BYTES = 64 * 1024
_WRITE_BYTES = 16 * 1024
_TIMEOUT_BASIS = (
    "TCP connect timeout; complete request-head send within timeout from first "
    "actual send attempt; response read inactivity timeout reset only by positive "
    "recv bytes; no JSON CPU deadline"
)


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """One direct loopback GET; bearer material is deliberately absent from repr."""

    base_url: str
    actor: str
    token: str = field(repr=False)
    run_id: str
    device_id: str
    page_size: int = 100
    streams: str | None = None

    def __post_init__(self) -> None:
        location = urlsplit(self.base_url)
        try:
            loopback = ipaddress.ip_address(location.hostname or "").is_loopback
            port = location.port
        except ValueError:
            raise ValueError("requires a direct loopback HTTP URL") from None
        if (
            location.scheme != "http"
            or not loopback
            or location.username is not None
            or location.password is not None
            or location.path not in ("", "/")
            or location.query
            or location.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError("requires a direct loopback HTTP URL")
        for value in (self.actor, self.run_id, self.device_id):
            if not isinstance(value, str) or not value or len(value) > 1024:
                raise ValueError("invalid request identity")
        if (
            not isinstance(self.token, str)
            or not self.token
            or len(self.token) > 8192
            or any(
                ord(character) < 33 or ord(character) > 126 for character in self.token
            )
        ):
            raise ValueError("invalid bearer material")
        if type(self.page_size) is not int or not 1 <= self.page_size <= 100:
            raise ValueError("invalid page size")
        if self.streams not in (None, "both", "windows", "alarms"):
            raise ValueError("invalid stream selection")


class ResponseFramingError(ValueError):
    """The controlled Content-Length response contract was not satisfied."""


class ResponseBudgetError(ValueError):
    """Known body bytes exceed the existing bounded wave evidence allowance."""


class _HeaderBuilder(HTTPConnection):
    """Exercise stdlib public header serialization into memory, never a socket."""

    def __init__(self, host: str, port: int | None) -> None:
        super().__init__(host, port)
        self.head = bytearray()

    def send(self, data: bytes) -> None:
        self.head.extend(data)


def _request_head(spec: RequestSpec) -> bytes:
    location = urlsplit(spec.base_url)
    parameters = [f"page_size={spec.page_size}"]
    if spec.streams is not None:
        parameters.append(f"streams={quote(spec.streams, safe='')}")
    target = (
        f"/v1/runs/{quote(spec.run_id, safe='')}/devices/"
        f"{quote(spec.device_id, safe='')}/health?" + "&".join(parameters)
    )
    builder = _HeaderBuilder(location.hostname, location.port)
    builder.putrequest("GET", target)
    builder.putheader("Authorization", f"Bearer {spec.token}")
    builder.putheader("Connection", "close")
    builder.endheaders()
    if len(builder.head) > MAX_HEADER_BYTES:
        raise ValueError("request head exceeds bounded size")
    return bytes(builder.head)


@dataclass(repr=False)
class _Response:
    head: bytearray = field(default_factory=bytearray)
    body: bytearray = field(default_factory=bytearray)
    status: int = 0
    length: int | None = None
    head_complete: bool = False

    def feed(self, data: bytes) -> bool:
        if self.head_complete:
            self.body.extend(data)
        else:
            self.head.extend(data)
            boundary = self.head.find(b"\r\n\r\n")
            if boundary < 0:
                if len(self.head) > MAX_HEADER_BYTES:
                    raise ResponseFramingError("response head too large")
                return False
            self.head_complete = True
            self.body.extend(self.head[boundary + 4 :])
            del self.head[boundary + 4 :]
            if boundary + 4 > MAX_HEADER_BYTES:
                raise ResponseFramingError("response head too large")
            self._parse_head(bytes(self.head[:boundary]))
        if self.length is not None and len(self.body) > self.length:
            raise ResponseFramingError("response exceeds declared length")
        return self.length is not None and len(self.body) == self.length

    def _parse_head(self, head: bytes) -> None:
        lines = head.split(b"\r\n")
        status_line = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?", lines[0])
        if status_line is None:
            raise ResponseFramingError("invalid HTTP status line")
        self.status = int(status_line.group(1))
        if not 200 <= self.status <= 599:
            raise ResponseFramingError("unsupported HTTP response status")
        fields: dict[bytes, list[bytes]] = {}
        for line in lines[1:]:
            key, separator, value = line.partition(b":")
            if not separator or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
                raise ResponseFramingError("invalid HTTP header")
            if any(byte < 32 and byte != 9 or byte == 127 for byte in value):
                raise ResponseFramingError("invalid HTTP header value")
            fields.setdefault(key.lower(), []).append(value.strip(b" \t"))
        lengths = fields.get(b"content-length", [])
        if (
            b"transfer-encoding" in fields
            or len(lengths) != 1
            or not re.fullmatch(rb"[0-9]+", lengths[0])
        ):
            raise ResponseFramingError("requires one Content-Length")
        self.length = int(lengths[0])
        if self.length > MAX_BODY_BYTES:
            raise ResponseBudgetError("response body too large")


@dataclass(repr=False)
class _Pending:
    spec: RequestSpec
    preparation: dict[str, Any] = field(default_factory=dict)
    connection: MeteredHTTPConnection | None = None
    reference: dict[str, Any] | None = None
    head: bytes = b""
    sent: int = 0
    response: _Response = field(default_factory=_Response)
    decoded: dict[str, object] = field(default_factory=dict)
    started: float | None = None
    waiting: float | None = None
    send_deadline: float | None = None
    read_deadline: float | None = None
    done: bool = False
    registered: bool = False

    def __post_init__(self) -> None:
        self.preparation = {
            "actor_ref": self.spec.actor,
            "launch_mode": "selector",
            "connect_started_ns": None,
            "connected_ns": None,
            "headers_prepared_ns": None,
            "launch_released_ns": None,
            "send_started_ns": None,
            "send_completed_ns": None,
            "first_receive_started_ns": None,
            "body_complete_ns": None,
            "decoded_ns": None,
            "http_started": False,
            "request_sent": False,
        }

    @property
    def sock(self) -> Any:
        return self.connection.sock


class _Dispatcher:
    def __init__(
        self,
        specs: Sequence[RequestSpec],
        accounting: RunAccounting,
        meter: SocketNetworkMeter,
        confirm: Callable[[list[dict]], Any],
        emit: Callable[[ClientRecord, dict], None],
        timeout: float,
    ) -> None:
        self.pending = [_Pending(spec) for spec in specs]
        self.accounting, self.meter = accounting, meter
        self.confirm, self.emit, self.timeout = confirm, emit, timeout
        self.selector = selectors.DefaultSelector()
        for item in self.pending:
            item.preparation["selector_backend"] = type(self.selector).__name__
        self.results: list[tuple[int, dict[str, object], str]] = []
        self.body_bytes = 0
        self.launch_deadline = 0.0

    def prepare(self) -> None:
        for item in self.pending:
            location = urlsplit(item.spec.base_url)
            item.preparation["connect_started_ns"] = time.perf_counter_ns()
            item.connection = MeteredHTTPConnection(
                location.hostname,
                location.port,
                timeout=self.timeout,
                network_meter=self.meter,
            )
            item.connection.connect()
            local, peer = item.sock.getsockname(), item.sock.getpeername()
            item.reference = {
                "local": {"host": str(local[0]), "port": int(local[1])},
                "peer": {"host": str(peer[0]), "port": int(peer[1])},
            }
            item.preparation["connected_ns"] = time.perf_counter_ns()
            item.head = _request_head(item.spec)
            item.preparation["headers_prepared_ns"] = time.perf_counter_ns()
            item.sock.setblocking(False)
        self.confirm([deepcopy(item.reference) for item in self.pending])
        released = time.perf_counter_ns()
        self.launch_deadline = time.perf_counter() + self.timeout
        for item in self.pending:
            item.preparation["launch_released_ns"] = released
            self.selector.register(
                item.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, item
            )
            item.registered = True

    def finish(self, item: _Pending, error: str = "") -> None:
        if item.done:
            return
        item.done = True
        completed = time.perf_counter()
        cleanup_failure = None
        if item.registered:
            try:
                self.selector.unregister(item.sock)
            except BaseException as exc:
                error = error or type(exc).__name__
                cleanup_failure = exc
            item.registered = False
        if item.connection is not None:
            owned_socket = item.sock
            try:
                item.connection.close()
            except BaseException as exc:
                error = error or type(exc).__name__
                if cleanup_failure is None or not isinstance(exc, Exception):
                    cleanup_failure = exc
                # HTTPConnection clears .sock before calling close. Retain our
                # local ownership for one bounded cleanup attempt, never resend.
                if owned_socket is not None:
                    try:
                        owned_socket.close()
                    except BaseException as retry_error:
                        if not isinstance(retry_error, Exception):
                            cleanup_failure = retry_error
        started = item.started if item.started is not None else completed
        record = ClientRecord(
            user_id=item.spec.actor,
            run_id=item.spec.run_id,
            device_id=item.spec.device_id,
            started_monotonic=started,
            waiting_monotonic=item.waiting if item.waiting is not None else completed,
            completed_monotonic=completed,
            status=item.response.status,
            server_request_id=(
                item.decoded.get("request_id", "")
                if isinstance(item.decoded.get("request_id", ""), str)
                else ""
            ),
            trace_id=(
                item.decoded.get("trace_id", "")
                if isinstance(item.decoded.get("trace_id", ""), str)
                else ""
            ),
            error=error,
            response_bytes_base64=base64.b64encode(item.response.body).decode("ascii"),
        )
        mapping = {
            "request_id": record.server_request_id,
            "connection_ref": deepcopy(item.reference),
            "request_context": {
                "streams": item.spec.streams,
                "page_size": item.spec.page_size,
                "first_page_streams": [
                    name
                    for name in ("windows", "alarms")
                    if item.spec.streams in (None, "both", name)
                ],
            },
            "preparation": deepcopy(item.preparation),
            "latency_eligible": item.started is not None,
            "latency_basis": (
                "first actual send attempt to complete read and JSON decode; "
                "TCP preparation/control acknowledgement/launch excluded"
                if item.started is not None
                else "not an HTTP start; terminal placeholder attempt only"
            ),
            "timeout_seconds": self.timeout,
            "timeout_basis": _TIMEOUT_BASIS,
        }
        item.head = b""
        if item.started is not None:
            self.accounting.client_complete(deepcopy(record))
        self.results.append((record.status, item.decoded, item.spec.run_id))
        try:
            self.emit(record, mapping)
        except BaseException:
            if cleanup_failure is not None:
                raise cleanup_failure
            raise
        if cleanup_failure is not None:
            raise cleanup_failure

    def write(self, item: _Pending) -> None:
        if item.started is None:
            item.started = self.accounting.client_start()
            item.preparation["send_started_ns"] = time.perf_counter_ns()
            item.preparation["http_started"] = True
            item.send_deadline = time.perf_counter() + self.timeout
        elif time.perf_counter() >= item.send_deadline:
            raise TimeoutError("request-head send deadline expired")
        try:
            sent = item.sock.send(
                memoryview(item.head)[item.sent : item.sent + _WRITE_BYTES]
            )
        except BlockingIOError as exc:
            if is_would_block(exc):
                return
            raise
        if sent <= 0:
            raise ConnectionError("request send made no progress")
        item.sent += sent
        if item.sent == len(item.head):
            item.preparation["request_sent"] = True
            item.preparation["send_completed_ns"] = time.perf_counter_ns()
            item.waiting = time.perf_counter()
            item.read_deadline = item.waiting + self.timeout
            self.selector.modify(item.sock, selectors.EVENT_READ, item)
        # A successful return proves bytes, not compliance with sendall's total
        # timeout. Keep request_sent/send_completed facts if the final write was
        # late, but do not turn an expired send deadline into a new read budget.
        if time.perf_counter() >= item.send_deadline:
            raise TimeoutError("request-head send deadline expired")

    def read(self, item: _Pending) -> None:
        if item.preparation["first_receive_started_ns"] is None:
            item.preparation["first_receive_started_ns"] = time.perf_counter_ns()
        try:
            data = item.sock.recv(_READ_BYTES)
        except BlockingIOError as exc:
            if is_would_block(exc):
                return
            raise
        if not data:
            raise EOFError("incomplete Content-Length response")
        item.read_deadline = time.perf_counter() + self.timeout
        previous = len(item.response.body)
        try:
            complete = item.response.feed(data)
        finally:
            self.body_bytes += len(item.response.body) - previous
        if self.body_bytes > MAX_BODY_BYTES:
            raise ResponseBudgetError("wave body budget exceeded")
        if not complete:
            return
        item.preparation["body_complete_ns"] = time.perf_counter_ns()
        error = ""
        try:
            decoded = json.loads(
                item.response.body.decode("utf-8"),
                parse_float=_finite_float,
                parse_constant=_invalid_constant,
            )
            if isinstance(decoded, dict):
                item.decoded = decoded
            else:
                error = "non_object_json"
        except (ValueError, UnicodeDecodeError):
            error = "invalid_json"
        item.preparation["decoded_ns"] = time.perf_counter_ns()
        if not error and item.response.status != 200:
            error = f"http_status_{item.response.status}"
        self.finish(item, error)

    def _ready_reads(self, ready: list) -> None:
        for key, mask in ready:
            item = key.data
            if mask & selectors.EVENT_READ and not item.done:
                try:
                    self.read(item)
                except Exception as exc:
                    if item.done or isinstance(exc, ResponseBudgetError):
                        raise
                    self.finish(item, type(exc).__name__)

    def _poll_reads(self) -> None:
        # Windows select rejects three empty fd sets after the final close.
        if self.selector.get_map():
            self._ready_reads(self.selector.select(0))

    def _deadline(self, item: _Pending) -> float:
        if item.started is None:
            return self.launch_deadline
        if not item.preparation["request_sent"]:
            return item.send_deadline
        return item.read_deadline

    def run(self) -> None:
        while any(not item.done for item in self.pending):
            active = [item for item in self.pending if not item.done]
            delay = max(
                0.0, min(self._deadline(item) for item in active) - time.perf_counter()
            )
            ready = self.selector.select(delay)
            self._ready_reads(ready)
            for key, mask in ready:
                item = key.data
                if mask & selectors.EVENT_WRITE and not item.done:
                    try:
                        self.write(item)
                    except Exception as exc:
                        self.finish(item, type(exc).__name__)
                    # A fresh readable response must not wait behind the rest of
                    # the previous batch's writable sockets, even for short writes.
                    self._poll_reads()
            # Ready data gets a fair read before inactivity is assessed. Only a
            # positive recv, never EAGAIN/readiness itself, extends read timeout.
            self._poll_reads()
            now = time.perf_counter()
            for item in self.pending:
                if not item.done and self._deadline(item) <= now:
                    self.finish(item, "TimeoutError")


def dispatch_wave(
    specs: Sequence[RequestSpec],
    *,
    accounting: RunAccounting,
    meter: SocketNetworkMeter,
    confirm_preparation: Callable[[list[dict]], Any],
    emit_completed: Callable[[ClientRecord, dict], None],
    timeout_seconds: float = 5,
) -> list[tuple[int, dict[str, object], str]]:
    """Prepare direct TCP/headers, confirm exact readers, then fairly dispatch.

    Each request's JSON decode and memory accounting completes immediately at
    its own terminal point. ``emit_completed`` must only enqueue to a bounded
    journal; it must not perform disk I/O in this loop. Pre-send failures emit
    terminal placeholder attempts but do not start/complete memory accounting.
    Preparation/control errors and process interrupts close all known sockets,
    retain every unfinished attempt once, and re-raise the original exception.
    """
    if (
        not 1 <= len(specs) <= 64
        or any(not isinstance(spec, RequestSpec) for spec in specs)
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("invalid bounded dispatch configuration")
    dispatcher = _Dispatcher(
        specs, accounting, meter, confirm_preparation, emit_completed, timeout_seconds
    )
    try:
        dispatcher.prepare()
        dispatcher.run()
    except BaseException as exc:
        for item in dispatcher.pending:
            if not item.done:
                try:
                    dispatcher.finish(item, type(exc).__name__)
                except BaseException:
                    # An earlier emit/interrupt remains primary. The parent
                    # journal owns its fallback for the already attempted emit.
                    pass
        raise
    finally:
        dispatcher.selector.close()
    return dispatcher.results
