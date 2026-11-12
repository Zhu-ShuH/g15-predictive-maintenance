"""Explicit per-socket accounting for plaintext HTTP application streams.

Only successful socket return values establish byte counts. These are stream
bytes, including HTTP headers/body, not TCP/IP headers, retransmissions or wire
utilization. No payload, HTTP header or exception message is retained. Wrapping
is explicit and local: no global socket/HTTP monkeypatch is installed.
"""

from __future__ import annotations

from copy import deepcopy
from http.client import HTTPConnection
import socket
import threading
import time
import os
from typing import Any
from urllib.request import HTTPHandler


def _address(value: Any) -> dict[str, Any] | None:
    if isinstance(value, tuple) and len(value) >= 2:
        return {"host": str(value[0]), "port": int(value[1])}
    return None


class SocketNetworkMeter:
    """Collect only metadata/counters from explicitly wrapped TCP sockets."""

    def __init__(self, endpoint: str) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("endpoint must be a nonempty label")
        self.endpoint = endpoint
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._next_id = 0

    def _attempt(self, requested_peer: Any = None) -> str:
        with self._lock:
            self._next_id += 1
            key = f"socket-{os.getpid()}-{self._next_id}"
            self._records[key] = {
                "connection_id": key,
                "endpoint": self.endpoint,
                "process_id": os.getpid(),
                "clock": "perf_counter_ns",
                "observation_started_monotonic_ns": time.perf_counter_ns(),
                "connected_monotonic_ns": None,
                "closed_monotonic_ns": None,
                "observation_basis": "connection attempt before TCP connect",
                "requested_peer": _address(requested_peer),
                "local": None,
                "peer": None,
                "connect_result": "pending",
                "close_result": "open",
                "bytes_sent_known": 0,
                "bytes_received_known": 0,
                "send_partial_unknown": False,
                "receive_partial_unknown": False,
                "io_events": [],
                "errors": [],
            }
            return key

    def _connect_failed(
        self, key: str, exc: BaseException, *, closed: bool = True
    ) -> None:
        with self._lock:
            row = self._records[key]
            if row["connect_result"] != "failed":
                row["errors"].append(
                    {"operation": "connect", "error_type": type(exc).__name__}
                )
            row["connect_result"] = "failed"
            row["close_result"] = "closed" if closed else "failed"
            row["closed_monotonic_ns"] = time.perf_counter_ns() if closed else None

    def wrap(
        self, sock: socket.socket, *, _attempt_id: str | None = None
    ) -> "MeteredSocket":
        """Wrap one connected socket, preserving its actual local/peer address.

        A post-accept/post-connect wrapper observes lifetime from this call, not
        from SYN or accept. Rewrapping the same proxy returns it without counting
        twice; transferring it to another meter is rejected.
        """
        if isinstance(sock, MeteredSocket):
            if sock._meter is not self:
                raise ValueError("socket already belongs to another meter")
            return sock
        key = _attempt_id or self._attempt()
        try:
            local, peer = _address(sock.getsockname()), _address(sock.getpeername())
            if (
                local is None
                or peer is None
                or sock.family not in (socket.AF_INET, socket.AF_INET6)
                or int(sock.type) & 0xF != int(socket.SOCK_STREAM)
            ):
                raise ValueError("requires a connected TCP stream socket")
        except BaseException as exc:
            # Ownership transfers on wrap: metadata failure cannot leak an
            # already accepted/connected raw socket or invent a successful close.
            try:
                sock.close()
            except BaseException as cleanup_error:
                self._connect_failed(key, exc, closed=False)
                self._closed(key, cleanup_error)
                if not isinstance(exc, Exception):
                    raise exc
                raise
            self._connect_failed(key, exc)
            raise
        with self._lock:
            row = self._records[key]
            row.update(
                local=local,
                peer=peer,
                connect_result="connected",
                connected_monotonic_ns=time.perf_counter_ns(),
            )
            if _attempt_id is None:
                row["observation_basis"] = (
                    "connected socket wrapped after TCP connect/accept"
                )
        return MeteredSocket(sock, self, key)

    def _io(
        self,
        key: str,
        operation: str,
        started: int,
        count: int | None,
        *,
        error: BaseException | None = None,
        peek: bool = False,
    ) -> None:
        observed_completion = time.perf_counter_ns()
        with self._lock:
            row = self._records[key]
            unknown = error is not None
            event = {
                "operation": operation,
                "started_monotonic_ns": started,
                "completed_monotonic_ns": observed_completion,
                "returned_bytes": count,
                "unknown_partial": unknown,
                "peek": peek,
                "error_type": type(error).__name__ if error is not None else None,
            }
            row["io_events"].append(event)
            sending = operation in {"send", "sendall"}
            if error is not None:
                row[
                    "send_partial_unknown" if sending else "receive_partial_unknown"
                ] = True
                row["errors"].append(
                    {"operation": operation, "error_type": type(error).__name__}
                )
            elif not peek:
                row["bytes_sent_known" if sending else "bytes_received_known"] += count

    def _closed(self, key: str, error: BaseException | None) -> None:
        with self._lock:
            row = self._records[key]
            if error is not None:
                row["close_result"] = "failed"
                row["errors"].append(
                    {"operation": "close", "error_type": type(error).__name__}
                )
            elif row["closed_monotonic_ns"] is None:
                row["close_result"] = "closed"
                row["closed_monotonic_ns"] = time.perf_counter_ns()

    @property
    def active_count(self) -> int:
        """Count pending attempts and sockets whose real close is not complete."""
        with self._lock:
            return sum(
                row["closed_monotonic_ns"] is None for row in self._records.values()
            )

    def snapshot(self) -> dict[str, Any]:
        """Return a payload-free copy, keeping unknown partials as unknown."""
        with self._lock:
            rows = deepcopy(list(self._records.values()))
            errors = [
                dict(connection_id=row["connection_id"], **error)
                for row in rows
                for error in row["errors"]
            ]
            info = time.get_clock_info("perf_counter")
            return {
                "format": "g15-socket-application-stream-v1",
                "endpoint": self.endpoint,
                "process_id": os.getpid(),
                "snapshot_monotonic_ns": time.perf_counter_ns(),
                "clock": {
                    "name": "perf_counter_ns",
                    "implementation": info.implementation,
                    "resolution_seconds": info.resolution,
                    "monotonic": info.monotonic,
                },
                "scope": "socket application stream bytes, including HTTP headers and body",
                "limitations": [
                    "excludes TCP/IP/link headers and retransmissions",
                    "does not establish link capacity, saturation or peer receipt",
                    "failed sendall/read partial bytes remain unknown; known counts are lower bounds",
                    "per-endpoint clocks are not subtracted across processes",
                ],
                "connections": rows,
                "connection_count": len(rows),
                "active_connections": sum(
                    row["closed_monotonic_ns"] is None for row in rows
                ),
                "closed_connections": sum(
                    row["closed_monotonic_ns"] is not None for row in rows
                ),
                "bytes_sent_known": sum(row["bytes_sent_known"] for row in rows),
                "bytes_received_known": sum(
                    row["bytes_received_known"] for row in rows
                ),
                "send_partial_unknown": any(
                    row["send_partial_unknown"] for row in rows
                ),
                "receive_partial_unknown": any(
                    row["receive_partial_unknown"] for row in rows
                ),
                "errors": errors,
            }

    def reset_closed(self) -> dict[str, Any]:
        """Return/reset a successful finished interval; never erase failed data.

        Call after all real sockets/file references close. An active connection
        or any error/unknown partial rejects reset, keeping every record intact.
        IDs remain unique across intervals. Failure requires retention and stop.
        """
        with self._lock:
            previous = self.snapshot()
            if previous["active_connections"]:
                raise RuntimeError("cannot reset active socket observations")
            if (
                previous["errors"]
                or previous["send_partial_unknown"]
                or previous["receive_partial_unknown"]
            ):
                raise RuntimeError("failed socket evidence must be retained")
            self._records.clear()
            return previous


class MeteredSocket:
    """Narrow TCP proxy for HTTP's send/sendall/recv/recv_into/makefile paths."""

    def __init__(
        self, sock: socket.socket, meter: SocketNetworkMeter, key: str
    ) -> None:
        self._sock, self._meter, self._key = sock, meter, key
        self._io_refs = 0
        self._closed = False
        self._really_closed = False
        self._close_lock = threading.RLock()

    def __getattr__(self, name: str) -> Any:
        if name in {
            "sendfile",
            "sendto",
            "sendmsg",
            "recvfrom",
            "recvfrom_into",
            "recvmsg",
            "recvmsg_into",
            "detach",
            "dup",
            "accept",
        }:
            raise NotImplementedError("operation is outside metered HTTP TCP scope")
        return getattr(self._sock, name)

    def send(self, data: Any, flags: int = 0) -> int:
        started = time.perf_counter_ns()
        try:
            count = self._sock.send(data, flags)
        except BaseException as exc:
            self._meter._io(self._key, "send", started, None, error=exc)
            raise
        self._meter._io(self._key, "send", started, count)
        return count

    def sendall(self, data: Any, flags: int = 0) -> None:
        count = memoryview(data).nbytes
        started = time.perf_counter_ns()
        try:
            self._sock.sendall(data, flags)
        except BaseException as exc:
            self._meter._io(self._key, "sendall", started, None, error=exc)
            raise
        self._meter._io(self._key, "sendall", started, count)

    def recv(self, size: int, flags: int = 0) -> bytes:
        started = time.perf_counter_ns()
        try:
            data = self._sock.recv(size, flags)
        except BaseException as exc:
            self._meter._io(self._key, "recv", started, None, error=exc)
            raise
        self._meter._io(
            self._key, "recv", started, len(data), peek=bool(flags & socket.MSG_PEEK)
        )
        return data

    def recv_into(self, buffer: Any, nbytes: int = 0, flags: int = 0) -> int:
        started = time.perf_counter_ns()
        try:
            count = self._sock.recv_into(buffer, nbytes, flags)
        except BaseException as exc:
            self._meter._io(self._key, "recv_into", started, None, error=exc)
            raise
        self._meter._io(
            self._key, "recv_into", started, count, peek=bool(flags & socket.MSG_PEEK)
        )
        return count

    def makefile(
        self,
        mode: str = "r",
        buffering: int | None = None,
        *,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        """Use CPython SocketIO over this proxy so buffered HTTP reads count once."""
        with self._close_lock:
            if self._closed:
                raise OSError("socket is closed")
            return socket.socket.makefile(
                self, mode, buffering, encoding=encoding, errors=errors, newline=newline
            )

    def _decref_socketios(self) -> None:
        # SocketIO owns one proxy reference, never an underlying raw SocketIO.
        with self._close_lock:
            if self._io_refs > 0:
                self._io_refs -= 1
            if self._closed and self._io_refs == 0:
                self._real_close()

    def _real_close(self) -> None:
        with self._close_lock:
            if self._really_closed:
                return
            self._really_closed = True
            error = None
            try:
                self._sock.close()
            except BaseException as exc:
                error = exc
                self._really_closed = False
                raise
            finally:
                self._meter._closed(self._key, error)

    def close(self) -> None:
        """Close exactly once, after buffered SocketIO references have closed."""
        with self._close_lock:
            self._closed = True
            if self._io_refs == 0:
                self._real_close()

    def __enter__(self) -> "MeteredSocket":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class MeteredHTTPConnection(HTTPConnection):
    """Explicit HTTP client factory that wraps before any HTTP bytes are sent."""

    def __init__(
        self, *args: Any, network_meter: SocketNetworkMeter, **kwargs: Any
    ) -> None:
        self.network_meter = network_meter
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        key = self.network_meter._attempt((self.host, self.port))
        try:
            if self._tunnel_host:
                raise ValueError(
                    "HTTP tunnels are outside metered plaintext HTTP scope"
                )
            super().connect()
            self.sock = self.network_meter.wrap(self.sock, _attempt_id=key)
        except BaseException as exc:
            try:
                if self.sock is not None:
                    self.sock.close()
                self.sock = None
            except BaseException as cleanup_error:
                self.network_meter._connect_failed(key, exc, closed=False)
                self.network_meter._closed(key, cleanup_error)
                if not isinstance(exc, Exception):
                    raise exc
                raise
            self.network_meter._connect_failed(key, exc)
            raise


class MeteredHTTPHandler(HTTPHandler):
    """Install with urllib.build_opener for a local, plaintext-HTTP-only client."""

    def __init__(self, *, network_meter: SocketNetworkMeter, **kwargs: Any) -> None:
        self.network_meter = network_meter
        super().__init__(**kwargs)

    def http_open(self, req: Any) -> Any:
        def connection(host: str, **kwargs: Any) -> MeteredHTTPConnection:
            return MeteredHTTPConnection(
                host, network_meter=self.network_meter, **kwargs
            )

        return self.do_open(connection, req)


def validate_network_pair(client: dict[str, Any], server: dict[str, Any]) -> list[str]:
    """Reconcile closed socket streams by actual reversed endpoint tuples.

    This proves recorded application-stream bytes agree at both ends, without
    claiming physical-wire usage or independently establishing PID isolation.
    Resource/origin PID checks and HTTP request-to-connection joins are separate.
    """
    problems: list[str] = []

    def integer(value: Any) -> bool:
        return type(value) is int and value >= 0

    def address(value: Any) -> tuple[str, int] | None:
        if (
            not isinstance(value, dict)
            or set(value) != {"host", "port"}
            or not isinstance(value.get("host"), str)
            or not value["host"]
            or type(value.get("port")) is not int
            or not 1 <= value["port"] <= 65535
        ):
            return None
        return value["host"], value["port"]

    def endpoint(snapshot: Any, label: str) -> dict[tuple, dict[str, Any]]:
        mapped: dict[tuple, dict[str, Any]] = {}
        if not isinstance(snapshot, dict):
            problems.append(f"{label} network snapshot missing")
            return mapped
        pid = snapshot.get("process_id")
        if (
            snapshot.get("endpoint") != label
            or type(pid) is not int
            or pid <= 0
            or snapshot.get("format") != "g15-socket-application-stream-v1"
        ):
            problems.append(f"{label} network origin invalid")
        clock = snapshot.get("clock")
        if not isinstance(clock, dict) or clock.get("name") != "perf_counter_ns":
            problems.append(f"{label} network clock invalid")
        if (
            snapshot.get("errors")
            or snapshot.get("send_partial_unknown") is not False
            or snapshot.get("receive_partial_unknown") is not False
        ):
            problems.append(f"{label} network contains failure/unknown partial")
        rows = snapshot.get("connections")
        if not isinstance(rows, list):
            problems.append(f"{label} network connections missing")
            return mapped
        if (
            type(snapshot.get("connection_count")) is not int
            or snapshot.get("connection_count") != len(rows)
            or type(snapshot.get("closed_connections")) is not int
            or snapshot.get("closed_connections") != len(rows)
            or type(snapshot.get("active_connections")) is not int
            or snapshot.get("active_connections") != 0
        ):
            problems.append(f"{label} network connection counts mismatch or active")
        totals = {"bytes_sent_known": 0, "bytes_received_known": 0}
        ids = set()
        for index, row in enumerate(rows):
            prefix = f"{label} connection[{index}]"
            if not isinstance(row, dict):
                problems.append(f"{prefix} malformed")
                continue
            key = row.get("connection_id")
            if not isinstance(key, str) or not key or key in ids:
                problems.append(f"{prefix} ID missing/duplicated")
            if isinstance(key, str):
                ids.add(key)
            if (
                row.get("endpoint") != label
                or type(row.get("process_id")) is not int
                or row.get("process_id") != pid
                or row.get("clock") != "perf_counter_ns"
            ):
                problems.append(f"{prefix} origin mismatch")
            times = [
                row.get(key)
                for key in (
                    "observation_started_monotonic_ns",
                    "connected_monotonic_ns",
                    "closed_monotonic_ns",
                )
            ]
            if not all(
                integer(value) and value > 0 for value in times
            ) or times != sorted(times):
                problems.append(f"{prefix} lifetime invalid/incomplete")
            if (
                row.get("connect_result") != "connected"
                or row.get("errors")
                or row.get("send_partial_unknown") is not False
                or row.get("receive_partial_unknown") is not False
            ):
                problems.append(f"{prefix} failed or partial unknown")
            local, peer = address(row.get("local")), address(row.get("peer"))
            if local is None or peer is None:
                problems.append(f"{prefix} actual endpoints missing")
            elif (local, peer) in mapped:
                problems.append(f"{prefix} duplicate/ambiguous endpoint tuple")
            else:
                mapped[(local, peer)] = row
            observed = {"bytes_sent_known": 0, "bytes_received_known": 0}
            events = row.get("io_events")
            if not isinstance(events, list):
                problems.append(f"{prefix} I/O events missing")
                events = []
            for event in events:
                if not isinstance(event, dict):
                    problems.append(f"{prefix} I/O event malformed")
                    continue
                operation, count = event.get("operation"), event.get("returned_bytes")
                start, end = event.get("started_monotonic_ns"), event.get(
                    "completed_monotonic_ns"
                )
                if (
                    not integer(start)
                    or not integer(end)
                    or start > end
                    or (
                        all(integer(value) for value in times)
                        and not times[1] <= start <= end <= times[2]
                    )
                ):
                    problems.append(f"{prefix} I/O clock invalid")
                if (
                    not isinstance(operation, str)
                    or operation not in {"send", "sendall", "recv", "recv_into"}
                    or not integer(count)
                    or event.get("unknown_partial") is not False
                    or event.get("error_type") is not None
                    or type(event.get("peek")) is not bool
                ):
                    problems.append(f"{prefix} I/O error/partial/type invalid")
                    continue
                if operation in {"send", "sendall"}:
                    if event["peek"]:
                        problems.append(f"{prefix} send cannot be a peek")
                    observed["bytes_sent_known"] += count
                elif not event["peek"]:
                    observed["bytes_received_known"] += count
            for field, count in observed.items():
                if not integer(row.get(field)) or row.get(field) != count:
                    problems.append(f"{prefix} {field} differs from I/O events")
                totals[field] += count
        for field, total in totals.items():
            if not integer(snapshot.get(field)) or snapshot.get(field) != total:
                problems.append(f"{label} {field} differs from connection events")
        return mapped

    client_rows, server_rows = endpoint(client, "client"), endpoint(server, "server")
    if not client_rows or not server_rows:
        problems.append("network lacks actual paired connections")
    if set(client_rows) != {(peer, local) for local, peer in server_rows}:
        problems.append("client/server actual endpoint tuples do not pair")
    for (local, peer), row in client_rows.items():
        other = server_rows.get((peer, local))
        if other is None:
            continue
        if row.get("bytes_sent_known") != other.get("bytes_received_known") or row.get(
            "bytes_received_known"
        ) != other.get("bytes_sent_known"):
            problems.append("paired socket application-stream bytes differ")
    return problems
