"""Opt-in Python runtime guard used by the offline acceptance tests.

Python imports ``sitecustomize`` during interpreter startup when this directory
is placed on ``PYTHONPATH``. The guard remains inert unless
``G15_DENY_NETWORK=1`` is present, so installing or importing the production
package does not change runtime behaviour.

This is a test aid, not an operating-system firewall. It rejects the standard
library socket audit events emitted by Python, but it cannot prove that the
host interface was disabled or that native code bypassing Python's audit API
made no network call.
"""

from __future__ import annotations

import os
import sys


class OfflineNetworkAccessError(RuntimeError):
    """Raised when guarded test code attempts to use Python networking."""


_NETWORK_AUDIT_EVENTS = frozenset(
    {
        "socket.__new__",
        "socket.bind",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.getnameinfo",
        "socket.getservbyname",
        "socket.getservbyport",
        "socket.sendmsg",
        "socket.sendto",
    }
)


def _deny_network(event: str, _arguments: tuple[object, ...]) -> None:
    """Reject Python socket operations while the opt-in guard is active."""
    if event in _NETWORK_AUDIT_EVENTS:
        raise OfflineNetworkAccessError(
            f"G15 offline guard blocked Python network event: {event}"
        )


if os.environ.get("G15_DENY_NETWORK") == "1":
    sys.addaudithook(_deny_network)
    os.environ["G15_OFFLINE_GUARD_ACTIVE"] = "1"
    if os.environ.get("G15_OFFLINE_GUARD_VERBOSE") == "1":
        print("G15 offline guard active", file=sys.stderr)
