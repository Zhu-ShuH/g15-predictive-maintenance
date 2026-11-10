"""The query router: authenticate, authorise, read, respond.

This is the thin transport layer of Issue #135 D0. It composes the g15.query
primitives into a single request handler with no network I/O: the caller hands
it a request (Authorization header plus target run/device/kind) and it returns
a response carrying a status code, a redaction-safe body and a Trace.

The error matrix is the whole of ADR-0007 5.1.4:

* authentication failures -> 401 with WWW-Authenticate: Bearer and an
  undifferentiated body
* authorisation failures and absent resources -> 404 (resource hidden)
* invalid parameters or cursors -> 400

There is deliberately no 403 in the first version. Every stage is recorded in
the trace; a rejected request records 'rejected' and stops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import time

from g15.pipeline.consumer import load_manifest_from_run_dir, ConsumptionError
from g15.query.authz import AuthzPolicy
from g15.query.bearer import verify_token
from g15.query.cursor import (
    CursorState,
    QUERY_ALARMS,
    QUERY_WINDOWS,
    decode_cursor,
    encode_cursor,
)
from g15.query.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidParameterError,
    QueryError,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_UNAUTHORIZED,
)
from g15.query import reader
from g15.service.audit import (
    STAGE_AUTHN_CHECKED,
    STAGE_AUTHZ_CHECKED,
    STAGE_DB_OPENED,
    STAGE_QUERY_COMPLETED,
    STAGE_QUERY_STARTED,
    STAGE_REJECTED,
    STAGE_RESPONSE_COMPLETED,
    Trace,
    new_trace,
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MIN_PAGE_SIZE = 1

#: The run directory layout, shared with the CLI (cli.py:57).
DATABASE_RELATIVE_PATH = Path("artifacts") / "results.sqlite"

_HEALTH_KIND = "health"
_KINDS = frozenset({QUERY_WINDOWS, QUERY_ALARMS, _HEALTH_KIND})

_ILLEGAL_RUN_CHARS = ("/", "\\", "..")


@dataclass(frozen=True)
class QueryRequest:
    authorization: str | None
    run_id: str
    device_id: str
    kind: str
    cursor: str | None = None
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass
class QueryResponse:
    status: int
    body: dict
    headers: dict = field(default_factory=dict)
    trace: Trace | None = None


class QueryRouter:
    """Authenticate, authorise and serve read-only queries over run databases.

    Args:
        key: The HMAC signing key (at least 32 bytes, supplied at runtime).
        policy: The loaded authorisation snapshot.
        runs: A mapping from run_id to the run directory holding its manifest.
        issuer: The issuer value the bearer claims must carry.
        audience: The audience value the bearer claims must carry.
    """

    def __init__(
        self,
        *,
        key: bytes,
        policy: AuthzPolicy,
        runs: Mapping[str, Path],
        issuer: str,
        audience: str,
        now: callable = None,
    ) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("signing key must be at least 32 bytes")
        if not issuer or not isinstance(issuer, str):
            raise ValueError("issuer must be a non-empty string")
        if not audience or not isinstance(audience, str):
            raise ValueError("audience must be a non-empty string")
        self._key = key
        self._policy = policy
        self._runs = dict(runs)
        self._issuer = issuer
        self._audience = audience
        self._now = now if now is not None else _unix_time

    def handle(self, request: QueryRequest) -> QueryResponse:
        trace = new_trace()
        try:
            subject = self._authenticate(request, trace)
            subject_entry = self._authorise(subject, request, trace)
            response = self._serve(subject, subject_entry, request, trace)
            trace.record(STAGE_RESPONSE_COMPLETED)
            response.trace = trace
            return response
        except QueryError as exc:
            trace.record(STAGE_REJECTED)
            return self._error_response(exc, trace)

    def _authenticate(self, request, trace):
        token = _bearer_from_header(request.authorization)
        subject = verify_token(
            token,
            self._key,
            issuer=self._issuer,
            audience=self._audience,
            now=self._now(),
        )
        trace.record(STAGE_AUTHN_CHECKED)
        return subject

    def _authorise(self, subject, request, trace):
        self._validate_target(request)
        subject_entry = self._policy.subject(subject.sub)
        if subject_entry is None:
            raise AuthorizationError("subject has no authorisation entry")
        if not subject_entry.allows(request.run_id, request.device_id):
            raise AuthorizationError("subject is not authorised for this resource")
        trace.record(STAGE_AUTHZ_CHECKED)
        return subject_entry

    def _validate_target(self, request):
        if request.kind not in _KINDS:
            raise InvalidParameterError("unknown query kind")
        if not _valid_identifier(request.run_id):
            raise InvalidParameterError("invalid run_id")
        if not _valid_identifier(request.device_id):
            raise InvalidParameterError("invalid device_id")
        if _contains_escape(request.run_id):
            raise InvalidParameterError("run_id contains a path escape")
        if request.kind != _HEALTH_KIND:
            page_size = request.page_size
            if isinstance(page_size, bool) or not isinstance(page_size, int):
                raise InvalidParameterError("page_size must be an integer")
            if page_size < MIN_PAGE_SIZE or page_size > MAX_PAGE_SIZE:
                raise InvalidParameterError("page_size is out of range")

    def _serve(self, _subject, subject_entry, request, trace):
        # Validate any cursor before touching the resource, so a tampered or
        # mismatched cursor surfaces as 400 even when the run would resolve.
        if request.cursor is not None:
            self._bind_cursor(request, subject_entry, request.kind)
        run_dir = self._runs.get(request.run_id)
        if run_dir is None:
            raise AuthorizationError("run is not available")
        self._verify_manifest(run_dir, request)
        database_path = Path(run_dir) / DATABASE_RELATIVE_PATH
        conn = reader.open_readonly(database_path)
        trace.record(STAGE_DB_OPENED)
        try:
            trace.record(STAGE_QUERY_STARTED)
            body = self._dispatch(subject_entry, request, conn)
            trace.record(STAGE_QUERY_COMPLETED)
            return QueryResponse(status=STATUS_OK, body=body)
        finally:
            conn.close()

    def _verify_manifest(self, run_dir, request):
        try:
            manifest = load_manifest_from_run_dir(Path(run_dir))
        except ConsumptionError as exc:
            raise AuthorizationError("run manifest is not available") from exc
        if manifest.run_id != request.run_id:
            raise AuthorizationError("run identity does not match")
        if manifest.device_id != request.device_id:
            raise AuthorizationError("device identity does not match")

    def _dispatch(self, subject_entry, request, conn):
        if request.kind == _HEALTH_KIND:
            view = reader.query_health(conn, request.run_id, request.device_id)
            if view is None:
                raise AuthorizationError("health state is not available")
            return {
                "kind": _HEALTH_KIND,
                "run_id": request.run_id,
                "device_id": request.device_id,
                "manifest_schema_version": view.manifest_schema_version,
                "state_schema_version": view.state_schema_version,
                "state": view.state,
            }
        if request.kind == QUERY_WINDOWS:
            return self._page_windows(subject_entry, request, conn)
        return self._page_alarms(subject_entry, request, conn)

    def _page_windows(self, subject_entry, request, conn):
        if request.cursor is None:
            high = reader.window_high_water(conn, request.run_id, request.device_id)
            if high is None:
                raise AuthorizationError("no windows are available")
            items, has_more = reader.query_windows(
                conn,
                request.run_id,
                request.device_id,
                high_water_start_seq=high,
                last_start_seq=None,
                page_size=request.page_size,
            )
            next_cursor = None
            if has_more and items:
                state = CursorState(
                    actor_ref=subject_entry.actor_ref,
                    policy_version=self._policy.policy_version,
                    query=QUERY_WINDOWS,
                    run_id=request.run_id,
                    device_id=request.device_id,
                    page_size=request.page_size,
                    window_high_water=high,
                    last_start_seq=items[-1].start_seq,
                )
                next_cursor = encode_cursor(state, self._key)
        else:
            state = self._bind_cursor(request, subject_entry, QUERY_WINDOWS)
            items, has_more = reader.query_windows(
                conn,
                request.run_id,
                request.device_id,
                high_water_start_seq=state.window_high_water,
                last_start_seq=state.last_start_seq,
                page_size=request.page_size,
            )
            next_cursor = None
            if has_more and items:
                next_state = CursorState(
                    actor_ref=state.actor_ref,
                    policy_version=state.policy_version,
                    query=QUERY_WINDOWS,
                    run_id=state.run_id,
                    device_id=state.device_id,
                    page_size=state.page_size,
                    window_high_water=state.window_high_water,
                    last_start_seq=items[-1].start_seq,
                )
                next_cursor = encode_cursor(next_state, self._key)
        return {
            "kind": QUERY_WINDOWS,
            "run_id": request.run_id,
            "device_id": request.device_id,
            "page_size": request.page_size,
            "has_more": has_more,
            "windows": [item.__dict__ for item in items],
            "next_cursor": next_cursor,
        }

    def _page_alarms(self, subject_entry, request, conn):
        if request.cursor is None:
            high = reader.alarm_high_water(conn, request.run_id, request.device_id)
            if high is None:
                raise AuthorizationError("no alarms are available")
            high_start, high_rule = high
            items, has_more = reader.query_alarms(
                conn,
                request.run_id,
                request.device_id,
                high_water_start_seq=high_start,
                high_water_rule_id=high_rule,
                last_start_seq=None,
                last_rule_id=None,
                page_size=request.page_size,
            )
            next_cursor = None
            if has_more and items:
                state = CursorState(
                    actor_ref=subject_entry.actor_ref,
                    policy_version=self._policy.policy_version,
                    query=QUERY_ALARMS,
                    run_id=request.run_id,
                    device_id=request.device_id,
                    page_size=request.page_size,
                    alarm_high_water_start_seq=high_start,
                    alarm_high_water_rule_id=high_rule,
                    last_start_seq=items[-1].start_seq,
                    last_rule_id=items[-1].rule_id,
                )
                next_cursor = encode_cursor(state, self._key)
        else:
            state = self._bind_cursor(request, subject_entry, QUERY_ALARMS)
            items, has_more = reader.query_alarms(
                conn,
                request.run_id,
                request.device_id,
                high_water_start_seq=state.alarm_high_water_start_seq,
                high_water_rule_id=state.alarm_high_water_rule_id,
                last_start_seq=state.last_start_seq,
                last_rule_id=state.last_rule_id,
                page_size=request.page_size,
            )
            next_cursor = None
            if has_more and items:
                next_state = CursorState(
                    actor_ref=state.actor_ref,
                    policy_version=state.policy_version,
                    query=QUERY_ALARMS,
                    run_id=state.run_id,
                    device_id=state.device_id,
                    page_size=state.page_size,
                    alarm_high_water_start_seq=state.alarm_high_water_start_seq,
                    alarm_high_water_rule_id=state.alarm_high_water_rule_id,
                    last_start_seq=items[-1].start_seq,
                    last_rule_id=items[-1].rule_id,
                )
                next_cursor = encode_cursor(next_state, self._key)
        return {
            "kind": QUERY_ALARMS,
            "run_id": request.run_id,
            "device_id": request.device_id,
            "page_size": request.page_size,
            "has_more": has_more,
            "alarms": [item.__dict__ for item in items],
            "next_cursor": next_cursor,
        }

    def _bind_cursor(self, request, subject_entry, kind):
        state = decode_cursor(request.cursor, self._key)
        if state.query != kind:
            raise InvalidParameterError("cursor kind does not match")
        if state.run_id != request.run_id or state.device_id != request.device_id:
            raise InvalidParameterError("cursor target does not match")
        if state.actor_ref != subject_entry.actor_ref:
            raise InvalidParameterError("cursor actor does not match")
        if state.policy_version != self._policy.policy_version:
            raise InvalidParameterError("cursor policy version does not match")
        if state.page_size != request.page_size:
            raise InvalidParameterError("cursor page size does not match")
        if kind == QUERY_WINDOWS and state.last_start_seq is None:
            raise InvalidParameterError("cursor lacks a last window key")
        if kind == QUERY_ALARMS and (
            state.last_start_seq is None or state.last_rule_id is None
        ):
            raise InvalidParameterError("cursor lacks a last alarm key")
        return state

    def _error_response(self, exc, trace):
        headers = {}
        body = {"status": exc.status}
        if exc.status == STATUS_UNAUTHORIZED:
            headers["WWW-Authenticate"] = "Bearer"
            body["error"] = "unauthorized"
        elif exc.status == STATUS_NOT_FOUND:
            body["error"] = "not_found"
        else:
            body["error"] = "invalid_request"
        return QueryResponse(status=exc.status, body=body, headers=headers, trace=trace)


def _bearer_from_header(header):
    if not isinstance(header, str) or not header:
        raise AuthenticationError("missing Authorization header")
    parts = header.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthenticationError("malformed Authorization header")
    return parts[1]


def _valid_identifier(value):
    return isinstance(value, str) and bool(value) and value.isascii()


def _contains_escape(value):
    return any(token in value for token in _ILLEGAL_RUN_CHARS)


def _unix_time():
    return int(time.time())
