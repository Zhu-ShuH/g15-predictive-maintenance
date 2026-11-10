"""Read-only authorised query layer for the G15 run database.

This package implements the D0 subset of Issue #135: a read-only application
query over the committed SQLite state, with HMAC bearer authentication and a
static YAML authorisation snapshot. It never reads private_truth and never
writes to the database; the only open path is connect_readonly().

The thin transport layer lives in g15.service and maps the QueryError family
onto the status matrix in ADR-0007 section 5.1.4.
"""

from g15.query.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidParameterError,
    QueryError,
    STATUS_BAD_REQUEST,
    STATUS_NOT_FOUND,
    STATUS_UNAUTHORIZED,
)

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "InvalidParameterError",
    "QueryError",
    "STATUS_BAD_REQUEST",
    "STATUS_NOT_FOUND",
    "STATUS_UNAUTHORIZED",
]
