"""Query-layer errors carrying the external status the transport surfaces.

The three-way matrix in ADR-0007 section 5.1.4 is the contract for the D0
subset: authentication failures surface as 401, authorization and
resource-existence failures as 404 (the resource is hidden, never disclosed),
and structurally invalid parameters or cursors as 400. The first version has
no 403 on purpose.
"""

from __future__ import annotations

#: External status codes surfaced by the thin transport layer.
STATUS_UNAUTHORIZED = 401
STATUS_NOT_FOUND = 404
STATUS_OK = 200
STATUS_BAD_REQUEST = 400


class QueryError(Exception):
    """Base error carrying the external status the transport must surface.

    Attributes:
        status: The HTTP-style status code for this failure.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class AuthenticationError(QueryError):
    """The bearer could not authenticate the caller (401).

    The transport must send 'WWW-Authenticate: Bearer' and a body that does
    not distinguish the concrete reason (missing header, bad scheme, tampered
    token, expired token, clock out of tolerance). The message stays internal;
    only the status leaves the service.
    """

    def __init__(self, message: str) -> None:
        super().__init__(STATUS_UNAUTHORIZED, message)


class AuthorizationError(QueryError):
    """The authenticated caller may not access the resource, or it is absent.

    Maps to 404 so the service never discloses whether a run or device exists.
    The concrete reason stays in the service log, never in the response body.
    """

    def __init__(self, message: str) -> None:
        super().__init__(STATUS_NOT_FOUND, message)


class InvalidParameterError(QueryError):
    """A parameter or cursor is structurally invalid (400, candidate)."""

    def __init__(self, message: str) -> None:
        super().__init__(STATUS_BAD_REQUEST, message)
