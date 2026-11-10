"""Thin read-only transport layer for the G15 query service.

Maps the g15.query application layer onto the D0 status matrix. There is no
network server here: the transport is the service object an in-process caller
constructs, and the bearer is read from the Authorization header field the
caller supplies. Error mapping is the only responsibility this layer adds.
"""

from g15.service.router import QueryRouter
from g15.service.audit import Trace, TraceEvent, new_trace

__all__ = [
    "QueryRouter",
    "Trace",
    "TraceEvent",
    "new_trace",
]
