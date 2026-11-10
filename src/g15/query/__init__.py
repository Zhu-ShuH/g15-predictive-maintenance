"""Authenticated, read-only business queries for committed G15 results.

The package deliberately contains no login endpoint and no writable repository.
Callers inject short-lived bearer and cursor keys at runtime and provide a
validated authorization snapshot before serving requests.
"""

from g15.query.application import (
    AuthenticationError,
    HealthQueryApplication,
    InvalidQueryError,
    QueryError,
    QueryResponse,
    ResourceNotFoundError,
)
from g15.query.auth import (
    AuthorizationPolicy,
    PolicyError,
    TokenClaims,
    TokenError,
    TokenVerifier,
    issue_token,
)
from g15.query.cursor import CursorCodec

__all__ = [
    "AuthenticationError",
    "AuthorizationPolicy",
    "CursorCodec",
    "HealthQueryApplication",
    "InvalidQueryError",
    "PolicyError",
    "QueryError",
    "QueryResponse",
    "ResourceNotFoundError",
    "TokenClaims",
    "TokenError",
    "TokenVerifier",
    "issue_token",
]
