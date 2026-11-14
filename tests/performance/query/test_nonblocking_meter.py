"""Nonblocking socket progress is distinct from EOF and unknown I/O failure."""

from contextlib import contextmanager
from copy import deepcopy
import errno
import json
import select
import socket

import pytest

from tools.query_load import network


@contextmanager
def tcp_pair():
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    left = socket.create_connection(listener.getsockname(), timeout=2)
    right, _ = listener.accept()
    listener.close()
    left.settimeout(2)
    right.settimeout(2)
    try:
        yield left, right
    finally:
        left.close()
        right.close()


def read_exact(sock, size):
    parts = []
    while size:
        part = sock.recv(min(size, 65536))
        assert part
        parts.append(part)
        size -= len(part)
    return b"".join(parts)


@pytest.mark.parametrize("operation", ["recv", "recv_into"])
def test_real_nonblocking_read_would_block_is_not_eof_or_failure(operation):
    with tcp_pair() as (left, right):
        client = network.SocketNetworkMeter("client")
        server = network.SocketNetworkMeter("server")
        a, b = client.wrap(left), server.wrap(right)
        a.setblocking(False)
        with pytest.raises(BlockingIOError):
            if operation == "recv":
                a.recv(16)
            else:
                a.recv_into(bytearray(16))
        pending = client.snapshot()
        event = pending["connections"][0]["io_events"][-1]
        assert event["would_block"] is True
        assert event["returned_bytes"] == 0
        assert event["unknown_partial"] is False
        assert event["error_type"] is None
        assert event["started_monotonic_ns"] <= event["completed_monotonic_ns"]
        assert pending["bytes_received_known"] == 0
        assert pending["errors"] == []
        assert pending["receive_partial_unknown"] is False
        with pytest.raises(RuntimeError, match="active"):
            client.reset_closed()
        b.sendall(b"private-response")
        assert select.select([a], [], [], 2)[0]
        assert a.recv(16) == b"private-response"
        b.close()
        assert select.select([a], [], [], 2)[0]
        assert a.recv(1) == b""
        assert (
            client.snapshot()["connections"][0]["io_events"][-1]["would_block"] is False
        )
        a.close()
        paired = client.snapshot(), server.snapshot()
        assert network.validate_network_pair(*paired) == []
        retained = client.reset_closed()
        assert retained["bytes_received_known"] == len(b"private-response")
        assert client.snapshot()["connection_count"] == 0
        assert "private-response" not in json.dumps(paired)


def test_real_nonblocking_backpressure_conserves_peer_bytes():
    with tcp_pair() as (left, right):
        left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        client = network.SocketNetworkMeter("client")
        server = network.SocketNetworkMeter("server")
        a, b = client.wrap(left), server.wrap(right)
        a.setblocking(False)
        payload = b"p" * (1024 * 1024)
        counts = []
        for _ in range(64):
            try:
                count = a.send(payload)
            except BlockingIOError:
                break
            assert count > 0
            counts.append(count)
        else:
            pytest.fail("loopback send buffer did not reach real backpressure")
        assert counts
        total = sum(counts)
        snapshot = client.snapshot()
        event = snapshot["connections"][0]["io_events"][-1]
        assert event["would_block"] is True
        assert event["returned_bytes"] == 0 and event["error_type"] is None
        assert snapshot["bytes_sent_known"] == total
        assert snapshot["send_partial_unknown"] is False
        assert snapshot["errors"] == []
        assert read_exact(b, total) == b"p" * total
        a.close()
        b.close()
        paired = client.snapshot(), server.snapshot()
        assert network.validate_network_pair(*paired) == []
        assert (
            paired[0]["bytes_sent_known"] == paired[1]["bytes_received_known"] == total
        )
        assert client.reset_closed()["bytes_sent_known"] == total


@pytest.mark.parametrize("code", sorted({errno.EAGAIN, errno.EWOULDBLOCK, 10035}))
def test_would_block_helper_accepts_only_explicit_blockingioerror_codes(code):
    assert network.is_would_block(BlockingIOError(code, "private-error"))


def ordinary_oserror_with_eagain():
    failure = OSError("private-error")
    failure.errno = errno.EAGAIN
    return failure


@pytest.mark.parametrize(
    "failure",
    [
        BlockingIOError(),
        BlockingIOError(errno.EINVAL, "private-error"),
        ordinary_oserror_with_eagain(),
        InterruptedError(errno.EINTR, "private-error"),
        KeyboardInterrupt("private-error"),
    ],
)
def test_would_block_helper_rejects_ambiguous_or_different_errors(failure):
    assert network.is_would_block(failure) is False


def test_would_block_helper_recognizes_explicit_windows_error_attribute():
    failure = BlockingIOError()
    failure.winerror = 10035
    assert network.is_would_block(failure) is True
    unrelated = OSError()
    unrelated.winerror = 10035
    assert network.is_would_block(unrelated) is False


@pytest.mark.parametrize("operation", ["send", "recv", "recv_into"])
@pytest.mark.parametrize(
    "mode,failure",
    [
        (None, BlockingIOError(errno.EAGAIN, "private-error")),
        (0.01, BlockingIOError(errno.EAGAIN, "private-error")),
        (0.0, BlockingIOError(errno.EINVAL, "private-error")),
        (0.0, InterruptedError(errno.EINTR, "private-error")),
        (0.0, OSError(errno.EIO, "private-error")),
        (0.0, KeyboardInterrupt("private-error")),
    ],
)
def test_other_io_errors_still_retain_unknown_and_prevent_reset(
    operation, mode, failure
):
    with tcp_pair() as (left, _right):
        left.settimeout(mode)

        class FailingSocket:
            def __getattr__(self, name):
                if name == operation:

                    def fail(*_args, **_kwargs):
                        raise failure

                    return fail
                return getattr(left, name)

        meter = network.SocketNetworkMeter("client")
        proxy = meter.wrap(FailingSocket())
        argument = (
            b"payload"
            if operation == "send"
            else (16 if operation == "recv" else bytearray(16))
        )
        with pytest.raises(type(failure)) as caught:
            getattr(proxy, operation)(argument)
        assert caught.value is failure
        proxy.close()
        snapshot = meter.snapshot()
        event = snapshot["connections"][0]["io_events"][-1]
        assert event["would_block"] is False
        assert event["returned_bytes"] is None
        assert event["unknown_partial"] is True
        assert event["error_type"] == type(failure).__name__
        assert snapshot["errors"]
        assert (
            snapshot[
                (
                    "send_partial_unknown"
                    if operation == "send"
                    else "receive_partial_unknown"
                )
            ]
            is True
        )
        with pytest.raises(RuntimeError, match="retained"):
            meter.reset_closed()
        assert "private" not in json.dumps(snapshot)


def test_nonblocking_sendall_eagain_preserves_unknown_partial():
    with tcp_pair() as (left, right):
        left.setblocking(False)

        class PartialSendall:
            def __getattr__(self, name):
                return getattr(left, name)

            def sendall(self, data, flags=0):
                assert left.send(data[:3], flags) == 3
                raise BlockingIOError(errno.EAGAIN, "private-partial")

        meter = network.SocketNetworkMeter("client")
        proxy = meter.wrap(PartialSendall())
        with pytest.raises(BlockingIOError):
            proxy.sendall(b"private-payload")
        assert right.recv(3) == b"pri"
        proxy.close()
        snapshot = meter.snapshot()
        event = snapshot["connections"][0]["io_events"][-1]
        assert event["would_block"] is False
        assert event["returned_bytes"] is None
        assert snapshot["bytes_sent_known"] == 0
        assert snapshot["send_partial_unknown"] is True
        with pytest.raises(RuntimeError, match="retained"):
            meter.reset_closed()


@pytest.mark.parametrize("damage", ["type", "nonzero", "sendall", "unknown", "error"])
def test_validation_rejects_forged_would_block_records(damage):
    with tcp_pair() as (left, right):
        client, server = network.SocketNetworkMeter(
            "client"
        ), network.SocketNetworkMeter("server")
        a, b = client.wrap(left), server.wrap(right)
        a.setblocking(False)
        with pytest.raises(BlockingIOError):
            a.recv(16)
        a.send(b"ok")
        assert b.recv(2) == b"ok"
        a.close()
        b.close()
        pair = client.snapshot(), server.snapshot()
        assert network.validate_network_pair(*pair) == []
        bad_client, bad_server = deepcopy(pair)
        event = bad_client["connections"][0]["io_events"][0]
        if damage == "type":
            event["would_block"] = "true"
        elif damage == "nonzero":
            # All byte sums and peer counts remain valid; only the forged
            # no-progress claim on a real two-byte send is inconsistent.
            event = bad_client["connections"][0]["io_events"][1]
            assert event["returned_bytes"] == 2
            event["would_block"] = True
        elif damage == "sendall":
            event["operation"] = "sendall"
        elif damage == "unknown":
            event["unknown_partial"] = True
        else:
            event["error_type"] = "BlockingIOError"
        assert network.validate_network_pair(bad_client, bad_server)


def test_short_send_counts_the_actual_bytes_forwarded_by_socket_adapter():
    """A short-return adapter transfers real bytes, without fabricating counts.

    Windows loopback can accept entire send buffers before EAGAIN; this test
    induces short returns explicitly, rather than claiming kernel short writes.
    """
    with tcp_pair() as (left, right):
        left.setblocking(False)

        class ShortSender:
            def __getattr__(self, name):
                return getattr(left, name)

            def send(self, data, flags=0):
                return left.send(memoryview(data)[:3], flags)

        client, server = network.SocketNetworkMeter(
            "client"
        ), network.SocketNetworkMeter("server")
        a, b = client.wrap(ShortSender()), server.wrap(right)
        payload = b"actual-short-send-bytes"
        remaining = memoryview(payload)
        counts = []
        while remaining:
            assert select.select([], [a], [], 2)[1]
            count = a.send(remaining)
            assert 0 < count <= 3
            counts.append(count)
            remaining = remaining[count:]
        assert len(counts) > 1 and sum(counts) == len(payload)
        assert read_exact(b, len(payload)) == payload
        a.close()
        b.close()
        snapshots = client.snapshot(), server.snapshot()
        assert network.validate_network_pair(*snapshots) == []
        events = snapshots[0]["connections"][0]["io_events"]
        assert [event["returned_bytes"] for event in events] == counts
        assert all(event["would_block"] is False for event in events)


def test_archived_blocking_io_without_new_field_remains_valid():
    with tcp_pair() as (left, right):
        client, server = network.SocketNetworkMeter(
            "client"
        ), network.SocketNetworkMeter("server")
        a, b = client.wrap(left), server.wrap(right)
        a.sendall(b"legacy")
        assert read_exact(b, 6) == b"legacy"
        a.close()
        b.close()
        pair = client.snapshot(), server.snapshot()
        for snapshot in pair:
            for row in snapshot["connections"]:
                for event in row["io_events"]:
                    assert event.pop("would_block") is False
        assert network.validate_network_pair(*pair) == []
