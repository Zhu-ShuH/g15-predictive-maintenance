"""Read-only application query over committed per-run SQLite results."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from g15.pipeline.schema import DataContractError, ManifestStatus, manifest_from_dict
from g15.query.auth import (
    AuthorizationPolicy,
    SubjectGrant,
    TokenClaims,
    TokenError,
    TokenVerifier,
)
from g15.query.cursor import (
    ALARM_SORT,
    WINDOW_SORT,
    CursorCodec,
    CursorError,
    CursorScope,
    CursorScopeError,
    CursorState,
)
from g15.storage.database import DatabaseError, connect_readonly
from g15.storage.repository import (
    StorageError,
    decode_features_json,
    decode_state_json,
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
QUERY_SHAPE = "single_run_single_device_health_v1"
_STATE_FIELDS = (
    "active_level",
    "consecutive_breaches",
    "normal_windows",
    "alarm_windows",
    "last_window_start_seq",
    "recovered_at_seq",
    "scoring_version",
    "rule_id",
)
_ALARM_HIGH_REFERENCE_PREFIX = "z:"
_ALARM_LAST_REFERENCE_PREFIX = "a:"
_SHA256_HEX_LENGTH = hashlib.sha256().digest_size * 2

EventSink = Callable[[Mapping[str, object]], None]
_LOGGER = logging.getLogger(__name__)


class QueryError(Exception):
    """Base class for safe query errors mapped by the transport."""


class AuthenticationError(QueryError):
    """Raised when the bearer credential is absent or invalid."""


class ResourceNotFoundError(QueryError):
    """Raised for every authorized-resource hiding outcome."""


class InvalidQueryError(QueryError):
    """Raised for non-secret parameter or malformed cursor errors."""


class QueryDataError(QueryError):
    """Raised when an authorized database violates its stored contract."""


@dataclass(frozen=True)
class QueryResponse:
    """JSON-ready response plus safe authorization context for observability."""

    body: Mapping[str, object]
    actor_ref: str
    permission_domain: str
    manifest_schema_version: str


@dataclass(frozen=True)
class _AuthenticatedPrincipal:
    """Opaque, verified identity passed only inside the query service."""

    claims: TokenClaims
    application_seal: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class _AuthorizedQuery:
    """Opaque authorization snapshot for one exact run/device pair."""

    run_id: str
    device_id: str
    grant: SubjectGrant
    application_seal: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class _RunSnapshot:
    manifest_schema_version: str
    run_scoring_version: str
    run_status: str
    windows: Mapping[str, object]
    alarms: Mapping[str, object]
    states: tuple[Mapping[str, object], ...]
    feature_versions: tuple[str, ...]
    state_versions: tuple[str, ...]
    state_last_end_seq_exclusive: int | None


class HealthQueryApplication:
    """Authorize and query one run/device through a real read-only connection."""

    def __init__(
        self,
        *,
        runs_root: Path | str,
        token_verifier: TokenVerifier,
        authorization_policy: AuthorizationPolicy,
        cursor_codec: CursorCodec,
        event_sink: EventSink | None = None,
        allow_live_runs: bool = False,
    ) -> None:
        if not isinstance(allow_live_runs, bool):
            raise ValueError("allow_live_runs must be a bool")
        self._allow_live_runs = allow_live_runs
        root = Path(runs_root)
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                "runs_root must be an existing controlled directory"
            ) from exc
        if not resolved_root.is_dir():
            raise ValueError("runs_root must be an existing controlled directory")
        if hmac.compare_digest(token_verifier.key, cursor_codec.key):
            raise ValueError("bearer and cursor keys must be purpose-separated")
        self._runs_root = resolved_root
        self._token_verifier = token_verifier
        self._policy = authorization_policy
        self._cursor_codec = cursor_codec
        self._event_sink = event_sink or _log_event
        self._authorization_seal = object()

    def query(
        self,
        *,
        authorization: str | None,
        run_id: str,
        device_id: str,
        request_id: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        windows_cursor: str | None = None,
        alarms_cursor: str | None = None,
    ) -> QueryResponse:
        """Return one stable page from each result stream.

        Authentication and authorization are repeated for every call. The two
        cursors are independent and each freezes its stream at the first-page
        high-water mark. Callers using a non-default page size must repeat it
        on later requests because it is part of both signed cursor scopes.
        """
        principal = self.authenticate(
            authorization=authorization,
            request_id=request_id,
        )
        authorized = self.authorize(
            principal=principal,
            run_id=run_id,
            device_id=device_id,
            request_id=request_id,
        )
        return self.query_authorized(
            authorized=authorized,
            request_id=request_id,
            page_size=page_size,
            windows_cursor=windows_cursor,
            alarms_cursor=alarms_cursor,
        )

    def authenticate(
        self,
        *,
        authorization: str | None,
        request_id: str,
    ) -> _AuthenticatedPrincipal:
        """Verify one bearer before the transport parses resource details."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be server-generated and non-empty")
        self._emit(request_id=request_id, stage="authn_checked", result="started")
        try:
            claims = self._authenticate(authorization)
        except AuthenticationError:
            self._emit(request_id=request_id, stage="rejected", result="unauthorized")
            raise
        self._emit(request_id=request_id, stage="authn_checked", result="accepted")
        return _AuthenticatedPrincipal(
            claims=claims,
            application_seal=self._authorization_seal,
        )

    def authorize(
        self,
        *,
        principal: _AuthenticatedPrincipal,
        run_id: str,
        device_id: str,
        request_id: str,
    ) -> _AuthorizedQuery:
        """Authorize the exact resource before parsing optional parameters."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be server-generated and non-empty")
        if (
            not isinstance(principal, _AuthenticatedPrincipal)
            or principal.application_seal is not self._authorization_seal
        ):
            raise AuthenticationError("authentication required")
        grant = self._policy.authorize(principal.claims.subject, run_id, device_id)
        if grant is None:
            self._emit(request_id=request_id, stage="rejected", result="not_found")
            raise ResourceNotFoundError("resource not found")
        self._emit(
            request_id=request_id,
            stage="authz_checked",
            result="accepted",
            grant=grant,
        )
        return _AuthorizedQuery(
            run_id=run_id,
            device_id=device_id,
            grant=grant,
            application_seal=self._authorization_seal,
        )

    def query_authorized(  # pylint: disable=too-many-locals
        self,
        *,
        authorized: _AuthorizedQuery,
        request_id: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        windows_cursor: str | None = None,
        alarms_cursor: str | None = None,
    ) -> QueryResponse:
        """Query one page from an opaque, exact authorization snapshot."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be server-generated and non-empty")
        if (
            not isinstance(authorized, _AuthorizedQuery)
            or authorized.application_seal is not self._authorization_seal
        ):
            raise AuthenticationError("authentication required")
        run_id = authorized.run_id
        device_id = authorized.device_id
        grant = authorized.grant
        try:
            size = _page_size(page_size)
            window_scope = _cursor_scope(
                grant=grant,
                policy_version=self._policy.policy_version,
                query_type="windows",
                run_id=run_id,
                device_id=device_id,
                sort=WINDOW_SORT,
                page_size=size,
            )
            alarm_scope = _cursor_scope(
                grant=grant,
                policy_version=self._policy.policy_version,
                query_type="alarms",
                run_id=run_id,
                device_id=device_id,
                sort=ALARM_SORT,
                page_size=size,
            )
            window_state = self._decode_cursor(windows_cursor, window_scope)
            alarm_state = self._decode_cursor(alarms_cursor, alarm_scope)
            run_dir, database_path, live_identity = self._resolve_resource(
                run_id, device_id
            )
        except ResourceNotFoundError:
            self._emit(
                request_id=request_id,
                stage="rejected",
                result="not_found",
                grant=grant,
            )
            raise
        except InvalidQueryError:
            self._emit(
                request_id=request_id,
                stage="rejected",
                result="invalid_request",
                grant=grant,
            )
            raise
        del run_dir  # The database path is the only resource used after validation.
        try:
            connection = connect_readonly(database_path)
        except DatabaseError as exc:
            self._emit(
                request_id=request_id,
                stage="rejected",
                result="not_found",
                grant=grant,
            )
            raise ResourceNotFoundError("resource not found") from exc
        self._emit(
            request_id=request_id,
            stage="db_opened",
            result="accepted",
            grant=grant,
        )

        try:
            self._emit(
                request_id=request_id,
                stage="query_started",
                result="started",
                grant=grant,
            )
            snapshot = self._read_snapshot(
                connection=connection,
                run_id=run_id,
                device_id=device_id,
                page_size=size,
                window_scope=window_scope,
                alarm_scope=alarm_scope,
                window_state=window_state,
                alarm_state=alarm_state,
                live_identity=live_identity,
            )
        except ResourceNotFoundError:
            self._emit(
                request_id=request_id,
                stage="rejected",
                result="not_found",
                grant=grant,
            )
            raise
        except (sqlite3.Error, StorageError, TypeError, ValueError, KeyError) as exc:
            self._emit(
                request_id=request_id,
                stage="rejected",
                result="invalid_committed_data",
                grant=grant,
            )
            raise QueryDataError("committed query data is invalid") from exc
        finally:
            connection.close()

        body: dict[str, object] = {
            "query_contract_version": "1",
            "run_id": run_id,
            "device_id": device_id,
            "versions": {
                "manifest_schema_version": snapshot.manifest_schema_version,
                "features_versions": list(snapshot.feature_versions),
                "state_schema_versions": list(snapshot.state_versions),
                "run_scoring_version": snapshot.run_scoring_version,
            },
            "run_status": snapshot.run_status,
            "committed_high_water": {
                "window_start_seq": snapshot.windows["high_water_start_seq"],
                "alarm": snapshot.alarms["high_water"],
                "state_last_end_seq_exclusive": (snapshot.state_last_end_seq_exclusive),
            },
            "windows": snapshot.windows,
            "alarms": snapshot.alarms,
            "states": list(snapshot.states),
        }
        self._emit(
            request_id=request_id,
            stage="query_completed",
            result="success",
            grant=grant,
            versions={
                "manifest_schema_version": snapshot.manifest_schema_version,
                "features_versions": list(snapshot.feature_versions),
                "state_schema_versions": list(snapshot.state_versions),
            },
        )
        return QueryResponse(
            body=body,
            actor_ref=grant.actor_ref,
            permission_domain=grant.permission_domain,
            manifest_schema_version=snapshot.manifest_schema_version,
        )

    def _authenticate(self, authorization: str | None) -> TokenClaims:
        if not isinstance(authorization, str):
            raise AuthenticationError("authentication required")
        if not authorization.startswith("Bearer "):
            raise AuthenticationError("authentication required")
        token = authorization[len("Bearer ") :]
        if not token or any(character.isspace() for character in token):
            raise AuthenticationError("authentication required")
        try:
            return self._token_verifier.verify(token)
        except TokenError as exc:
            raise AuthenticationError("authentication required") from exc

    def _decode_cursor(
        self, encoded: str | None, scope: CursorScope
    ) -> CursorState | None:
        if encoded is None:
            return None
        try:
            return self._cursor_codec.decode(encoded, expected=scope)
        except CursorScopeError as exc:
            raise ResourceNotFoundError("resource not found") from exc
        except CursorError as exc:
            raise InvalidQueryError("invalid cursor") from exc

    def _resolve_resource(
        self, run_id: str, device_id: str
    ) -> tuple[Path, Path, Mapping[str, str] | None]:
        """Resolve an authorized offline run or an explicitly opted-in live run."""
        try:
            run_dir = (self._runs_root / run_id).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ResourceNotFoundError("resource not found") from exc
        if not run_dir.is_dir() or not run_dir.is_relative_to(self._runs_root):
            raise ResourceNotFoundError("resource not found")

        live_identity = None
        live_path = run_dir / "live-session.json"
        manifest_path = run_dir / "manifest.json"
        try:
            if live_path.exists() or live_path.is_symlink():
                if (
                    not self._allow_live_runs
                    or manifest_path.exists()
                    or manifest_path.is_symlink()
                ):
                    raise ResourceNotFoundError("resource not found")
                # The separate descriptor never relaxes the complete-manifest
                # protocol. It is only accepted by an explicit live service.
                from g15.live.schema import load_live_session

                live = load_live_session(run_dir, run_id, device_id)
                live_identity = {
                    "schema_version": live["schema_version"],
                    "status": "streaming",
                    "scenario_id": live["scenario_id"],
                    "started_at": live["started_at_utc"],
                }
            else:
                manifest_path = manifest_path.resolve(strict=True)
                if not manifest_path.is_file() or not manifest_path.is_relative_to(
                    run_dir
                ):
                    raise ResourceNotFoundError("resource not found")
                raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = manifest_from_dict(raw_manifest)
                if (
                    manifest.status != ManifestStatus.COMPLETE
                    or manifest.run_id != run_id
                    or manifest.device_id != device_id
                ):
                    raise ResourceNotFoundError("resource not found")
        except ResourceNotFoundError:
            raise
        except (OSError, ValueError, TypeError, KeyError, DataContractError) as exc:
            raise ResourceNotFoundError("resource not found") from exc

        try:
            database_path = (run_dir / "artifacts" / "results.sqlite").resolve(
                strict=True
            )
        except (OSError, RuntimeError) as exc:
            raise ResourceNotFoundError("resource not found") from exc
        if not database_path.is_file() or not database_path.is_relative_to(run_dir):
            raise ResourceNotFoundError("resource not found")
        return run_dir, database_path, live_identity

    def _read_snapshot(  # pylint: disable=too-many-arguments
        self,
        *,
        connection: sqlite3.Connection,
        run_id: str,
        device_id: str,
        page_size: int,
        window_scope: CursorScope,
        alarm_scope: CursorScope,
        window_state: CursorState | None,
        alarm_state: CursorState | None,
        live_identity: Mapping[str, str] | None = None,
    ) -> _RunSnapshot:
        connection.execute("BEGIN")
        try:
            run_row = connection.execute(
                """
                SELECT schema_version, scoring_version, status, scenario_id, started_at
                FROM runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise ResourceNotFoundError("resource not found")
            if live_identity is None:
                if str(run_row[2]) != ManifestStatus.COMPLETE.value:
                    raise ResourceNotFoundError("resource not found")
            elif any(
                str(run_row[index]) != live_identity[field]
                for index, field in (
                    (0, "schema_version"),
                    (2, "status"),
                    (3, "scenario_id"),
                    (4, "started_at"),
                )
            ):
                raise ResourceNotFoundError("resource not found")
            windows, feature_versions = self._read_windows(
                connection,
                run_id,
                device_id,
                page_size,
                window_scope,
                window_state,
            )
            alarms = self._read_alarms(
                connection,
                run_id,
                device_id,
                page_size,
                alarm_scope,
                alarm_state,
            )
            states, state_versions, last_state_end = _read_states(
                connection, run_id, device_id
            )
            connection.execute("COMMIT")
        except Exception:  # pylint: disable=broad-exception-caught
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return _RunSnapshot(
            manifest_schema_version=str(run_row[0]),
            run_scoring_version=str(run_row[1]),
            run_status=str(run_row[2]),
            windows=windows,
            alarms=alarms,
            states=states,
            feature_versions=feature_versions,
            state_versions=state_versions,
            state_last_end_seq_exclusive=last_state_end,
        )

    def _read_windows(  # pylint: disable=too-many-arguments
        self,
        connection: sqlite3.Connection,
        run_id: str,
        device_id: str,
        page_size: int,
        scope: CursorScope,
        cursor: CursorState | None,
    ) -> tuple[Mapping[str, object], tuple[str, ...]]:
        high_water = (
            _window_high_water(connection, run_id, device_id)
            if cursor is None
            else int(cursor.high_water)
        )
        last_key = None if cursor is None else int(cursor.last_key)
        rows = _window_rows(
            connection,
            run_id,
            device_id,
            page_size + 1,
            high_water,
            last_key,
        )
        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        items: list[Mapping[str, object]] = []
        versions: set[str] = set()
        for row in page_rows:
            item = _window_item(row)
            items.append(item)
            versions.add(str(item["features_version"]))
        next_cursor = None
        if has_more and page_rows and high_water is not None:
            next_cursor = self._cursor_codec.encode(
                CursorState(
                    scope=scope,
                    high_water=high_water,
                    last_key=int(page_rows[-1][0]),
                )
            )
        return (
            {
                "sort": WINDOW_SORT,
                "page_size": page_size,
                "high_water_start_seq": high_water,
                "items": items,
                "next_cursor": next_cursor,
            },
            tuple(sorted(versions)),
        )

    def _read_alarms(  # pylint: disable=too-many-arguments
        self,
        connection: sqlite3.Connection,
        run_id: str,
        device_id: str,
        page_size: int,
        scope: CursorScope,
        cursor: CursorState | None,
    ) -> Mapping[str, object]:
        if cursor is None:
            high_water = _alarm_high_water(connection, run_id, device_id)
            last_key = None
        else:
            high_water = _resolve_alarm_cursor_reference(
                connection,
                run_id,
                device_id,
                cursor.high_water,
                _ALARM_HIGH_REFERENCE_PREFIX,
            )
            last_key = _resolve_alarm_cursor_reference(
                connection,
                run_id,
                device_id,
                cursor.last_key,
                _ALARM_LAST_REFERENCE_PREFIX,
            )
        rows = _alarm_rows(
            connection,
            run_id,
            device_id,
            page_size + 1,
            high_water,
            last_key,
        )
        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        items = tuple(_alarm_item(row) for row in page_rows)
        next_cursor = None
        if has_more and page_rows and high_water is not None:
            next_cursor = self._cursor_codec.encode(
                CursorState(
                    scope=scope,
                    high_water=_alarm_cursor_reference(
                        high_water,
                        _ALARM_HIGH_REFERENCE_PREFIX,
                    ),
                    last_key=_alarm_cursor_reference(
                        (int(page_rows[-1][0]), str(page_rows[-1][1])),
                        _ALARM_LAST_REFERENCE_PREFIX,
                    ),
                )
            )
        return {
            "sort": ALARM_SORT,
            "page_size": page_size,
            "high_water": (
                None
                if high_water is None
                else {"start_seq": high_water[0], "rule_id": high_water[1]}
            ),
            "items": list(items),
            "next_cursor": next_cursor,
        }

    def _emit(
        self,
        *,
        request_id: str,
        stage: str,
        result: str,
        grant: SubjectGrant | None = None,
        versions: Mapping[str, object] | None = None,
    ) -> None:
        event: dict[str, object] = {
            "timestamp_utc": _utc_now(),
            "request_id": request_id,
            "trace_id": request_id,
            "stage": stage,
            "query_shape": QUERY_SHAPE,
            "result": result,
        }
        if grant is not None:
            event["actor_ref"] = grant.actor_ref
            event["permission_domain"] = grant.permission_domain
        if versions is not None:
            event.update(versions)
        self._event_sink(event)


def _cursor_scope(
    *,
    grant: SubjectGrant,
    policy_version: str,
    query_type: str,
    run_id: str,
    device_id: str,
    sort: str,
    page_size: int,
) -> CursorScope:
    return CursorScope(
        actor_ref=grant.actor_ref,
        policy_version=policy_version,
        permission_domain=grant.permission_domain,
        query_type=query_type,
        run_id=run_id,
        device_id=device_id,
        sort=sort,
        page_size=page_size,
    )


def _page_size(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidQueryError("invalid page size")
    if not 1 <= value <= MAX_PAGE_SIZE:
        raise InvalidQueryError("invalid page size")
    return value


def _window_high_water(
    connection: sqlite3.Connection, run_id: str, device_id: str
) -> int | None:
    row = connection.execute(
        """
        SELECT MAX(start_seq) FROM feature_windows
        WHERE run_id = ? AND device_id = ?
        """,
        (run_id, device_id),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _alarm_high_water(
    connection: sqlite3.Connection, run_id: str, device_id: str
) -> tuple[int, str] | None:
    row = connection.execute(
        """
        SELECT start_seq, rule_id FROM alarms
        WHERE run_id = ? AND device_id = ?
        ORDER BY start_seq DESC, rule_id DESC LIMIT 1
        """,
        (run_id, device_id),
    ).fetchone()
    return None if row is None else (int(row[0]), str(row[1]))


def _window_rows(  # pylint: disable=too-many-arguments
    connection: sqlite3.Connection,
    run_id: str,
    device_id: str,
    limit: int,
    high_water: int | None,
    last_key: int | None,
) -> list[tuple[Any, ...]]:
    if high_water is None:
        return []
    if last_key is None:
        return connection.execute(
            """
            SELECT start_seq, end_seq_exclusive, features_json, health_score
            FROM feature_windows
            WHERE run_id = ? AND device_id = ? AND start_seq <= ?
            ORDER BY start_seq DESC LIMIT ?
            """,
            (run_id, device_id, high_water, limit),
        ).fetchall()
    return connection.execute(
        """
        SELECT start_seq, end_seq_exclusive, features_json, health_score
        FROM feature_windows
        WHERE run_id = ? AND device_id = ?
          AND start_seq <= ? AND start_seq < ?
        ORDER BY start_seq DESC LIMIT ?
        """,
        (run_id, device_id, high_water, last_key, limit),
    ).fetchall()


def _alarm_rows(  # pylint: disable=too-many-arguments
    connection: sqlite3.Connection,
    run_id: str,
    device_id: str,
    limit: int,
    high_water: tuple[int, str] | None,
    last_key: tuple[int, str] | None,
) -> list[tuple[Any, ...]]:
    if high_water is None:
        return []
    high_start, high_rule = high_water
    if last_key is None:
        return connection.execute(
            """
            SELECT start_seq, rule_id, level, reason, created_at
            FROM alarms
            WHERE run_id = ? AND device_id = ?
              AND (start_seq < ? OR (start_seq = ? AND rule_id <= ?))
            ORDER BY start_seq DESC, rule_id DESC LIMIT ?
            """,
            (run_id, device_id, high_start, high_start, high_rule, limit),
        ).fetchall()
    last_start, last_rule = last_key
    return connection.execute(
        """
        SELECT start_seq, rule_id, level, reason, created_at
        FROM alarms
        WHERE run_id = ? AND device_id = ?
          AND (start_seq < ? OR (start_seq = ? AND rule_id <= ?))
          AND (start_seq < ? OR (start_seq = ? AND rule_id < ?))
        ORDER BY start_seq DESC, rule_id DESC LIMIT ?
        """,
        (
            run_id,
            device_id,
            high_start,
            high_start,
            high_rule,
            last_start,
            last_start,
            last_rule,
            limit,
        ),
    ).fetchall()


def _window_item(row: tuple[Any, ...]) -> Mapping[str, object]:
    envelope = decode_features_json(str(row[2]))
    required = (
        "features_version",
        "sample_count",
        "sample_rate_hz",
        "window_start_seq",
        "scoring_version",
        "rule_id",
    )
    if any(name not in envelope for name in required):
        raise StorageError("features_json is missing query fields")
    start_seq = _stored_nonnegative_int(row[0], "start_seq")
    window_start = _stored_nonnegative_int(
        envelope["window_start_seq"], "window_start_seq"
    )
    if window_start != start_seq:
        raise StorageError("features_json window_start_seq disagrees with its row")
    end_seq = _stored_nonnegative_int(row[1], "end_seq_exclusive")
    sample_count = _stored_nonnegative_int(envelope["sample_count"], "sample_count")
    sample_rate = _stored_nonnegative_int(envelope["sample_rate_hz"], "sample_rate_hz")
    if end_seq <= start_seq or sample_count <= 0 or sample_rate <= 0:
        raise StorageError("features_json carries invalid window geometry")
    version = _stored_text(envelope["features_version"], "features_version")
    scoring_version = _stored_text(envelope["scoring_version"], "scoring_version")
    rule_id = _stored_text(envelope["rule_id"], "rule_id")
    health_score = float(row[3])
    if not 0.0 <= health_score <= 100.0:
        raise StorageError("health_score is outside [0, 100]")
    return {
        "start_seq": start_seq,
        "end_seq_exclusive": end_seq,
        "health_score": health_score,
        "features_version": version,
        "sample_count": sample_count,
        "sample_rate_hz": sample_rate,
        "scoring_version": scoring_version,
        "rule_id": rule_id,
    }


def _alarm_item(row: tuple[Any, ...]) -> Mapping[str, object]:
    start_seq = _stored_nonnegative_int(row[0], "alarm.start_seq")
    rule_id = _stored_text(row[1], "alarm.rule_id")
    level = _stored_text(row[2], "alarm.level")
    reason = _stored_text(row[3], "alarm.reason")
    created_at = _stored_text(row[4], "alarm.created_at")
    return {
        "start_seq": start_seq,
        "rule_id": rule_id,
        "level": level,
        "reason": reason,
        "created_at": created_at,
    }


def _read_states(
    connection: sqlite3.Connection, run_id: str, device_id: str
) -> tuple[tuple[Mapping[str, object], ...], tuple[str, ...], int | None]:
    rows = connection.execute(
        """
        SELECT rule_id, last_end_seq_exclusive, state_json
        FROM alarm_states
        WHERE run_id = ? AND device_id = ?
        ORDER BY rule_id
        """,
        (run_id, device_id),
    ).fetchall()
    items: list[Mapping[str, object]] = []
    versions: set[str] = set()
    last_end: int | None = None
    for row in rows:
        raw = _strict_stored_object(str(row[2]), "state_json")
        version = raw.get("state_schema_version")
        if not isinstance(version, str) or not version:
            raise StorageError("state_json has no valid state_schema_version")
        state = decode_state_json(str(row[2]))
        state_values = asdict(state)
        projected = {name: state_values[name] for name in _STATE_FIELDS}
        database_rule = str(row[0])
        if state.rule_id is not None and state.rule_id != database_rule:
            raise StorageError("state rule_id disagrees with its database row")
        end_seq = _stored_nonnegative_int(row[1], "alarm_states.last_end_seq_exclusive")
        items.append(
            {
                "rule_id": database_rule,
                "last_end_seq_exclusive": end_seq,
                "state_schema_version": version,
                "state": projected,
            }
        )
        versions.add(version)
        last_end = end_seq if last_end is None else max(last_end, end_seq)
    return tuple(items), tuple(sorted(versions)), last_end


def _strict_stored_object(text: str, label: str) -> dict[str, object]:
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise StorageError(f"{label} contains duplicate fields")
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=unique_pairs)
    except (TypeError, ValueError) as exc:
        raise StorageError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise StorageError(f"{label} must be a JSON object")
    return payload


def _alarm_cursor_reference(key: tuple[int, str], prefix: str) -> tuple[int, str]:
    """Return a fixed-size cursor reference for an arbitrarily long rule ID."""
    start_seq = _stored_nonnegative_int(key[0], "alarm.start_seq")
    rule_id = _stored_text(key[1], "alarm.rule_id")
    digest = hashlib.sha256(rule_id.encode("utf-8")).hexdigest()
    return start_seq, f"{prefix}{digest}"


def _resolve_alarm_cursor_reference(  # pylint: disable=too-many-arguments
    connection: sqlite3.Connection,
    run_id: str,
    device_id: str,
    value: int | tuple[int, str],
    expected_prefix: str,
) -> tuple[int, str]:
    """Resolve one signed fixed-size reference against the authorized database."""
    start_seq, encoded_reference = _as_alarm_reference_key(value)
    if not encoded_reference.startswith(expected_prefix):
        raise CursorError("invalid alarm cursor")
    digest = encoded_reference[len(expected_prefix) :]
    if (
        len(digest) != _SHA256_HEX_LENGTH
        or digest.lower() != digest
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise CursorError("invalid alarm cursor")

    rows = connection.execute(
        """
        SELECT rule_id FROM alarms
        WHERE run_id = ? AND device_id = ? AND start_seq = ?
        """,
        (run_id, device_id, start_seq),
    ).fetchall()
    matches: list[str] = []
    for row in rows:
        rule_id = _stored_text(row[0], "alarm.rule_id")
        candidate = hashlib.sha256(rule_id.encode("utf-8")).hexdigest()
        if hmac.compare_digest(candidate, digest):
            matches.append(rule_id)
    if len(matches) != 1:
        raise StorageError("alarm cursor reference is absent or ambiguous")
    return start_seq, matches[0]


def _as_alarm_reference_key(value: object) -> tuple[int, str]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise CursorError("invalid alarm cursor")
    start_seq, reference = value
    if (
        isinstance(start_seq, bool)
        or not isinstance(start_seq, int)
        or start_seq < 0
        or not isinstance(reference, str)
        or not reference
    ):
        raise CursorError("invalid alarm cursor")
    return start_seq, reference


def _stored_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageError(f"{label} must be a non-negative integer")
    return value


def _stored_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StorageError(f"{label} must be non-empty text")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log_event(event: Mapping[str, object]) -> None:
    """Emit one already-redacted JSON event through standard logging."""
    _LOGGER.info(
        "%s",
        json.dumps(
            event,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )
