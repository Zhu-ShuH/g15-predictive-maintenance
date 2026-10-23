"""Repository for the consumer's SQLite access (Issue #8, owner C2).

The repository is the only place that turns analysis results into rows. It
owns four things:

* the row shapes of the five section 9 tables;
* the JSON envelopes used for ``feature_windows.features_json`` and
  ``alarm_states.state_json``;
* the idempotency rules -- which rows are skipped on a repeat, and which
  conflict is refused outright;
* the transaction boundaries, one per shard.

The two envelopes
----------------

``state_json`` stores the alarm state. It is a *envelope* rather than a flat
mapping so that three different kinds of information never get confused:

``state_schema_version``
    The format version. ``"1"`` is the current one. A missing value means a
    legacy row written before the version existed; an unrecognised value is
    refused rather than guessed at, because reading a future envelope with the
    current rules would silently drop fields.

``state``
    Exactly the eight fields of B2's ``AlarmState``, under their own names.
    No more, no fewer: an unexpected key is refused, so a typo in a key name
    cannot look like a valid state.

``reset_event``
    The audit trail of an explicit streak reset: when, why, and what the state
    looked like before. This is the storage layer's own area -- B2's dataclass
    does not know about it.

``features_json`` uses the same idea: a ``features_version``, the eight
feature values under their own names, the window's sample count and rate, the
window position, the rule identity, and a human-readable explanation of the
score.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from g15.analysis.alarms import Alarm, AlarmState
from g15.analysis.features import FEATURE_NAMES, FeatureRecord
from g15.analysis.health import HealthResult

from g15.storage.database import transaction

__all__ = [
    "FEATURES_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION",
    "StorageError",
    "ChunkConflictError",
    "StateCompatibilityError",
    "WindowRecord",
    "ConsumptionRepository",
    "encode_features_json",
    "decode_features_json",
    "encode_state_json",
    "decode_state_json",
    "utc_now_text",
]


#: Version of the ``features_json`` envelope. Bump it when the set of keys or
#: their meaning changes; an unknown version is refused on read.
FEATURES_SCHEMA_VERSION = "1"

#: Version of the ``state_json`` envelope, as proposed in Issue #57.
STATE_SCHEMA_VERSION = "1"

#: The eight keys of ``AlarmState``, in the order B2 declares them.
_STATE_FIELDS: tuple[str, ...] = (
    "active_level",
    "consecutive_breaches",
    "normal_windows",
    "alarm_windows",
    "last_window_start_seq",
    "recovered_at_seq",
    "scoring_version",
    "rule_id",
)

#: The count fields, which must be JSON integers -- never floats, never bools.
_STATE_COUNTERS: frozenset[str] = frozenset(
    {"consecutive_breaches", "normal_windows", "alarm_windows"}
)

#: The position fields, which are an integer or null.
_STATE_POSITIONS: frozenset[str] = frozenset(
    {"last_window_start_seq", "recovered_at_seq"}
)


class StorageError(Exception):
    """Raised when a row cannot be written or read as the contract requires."""


class ChunkConflictError(StorageError):
    """Raised when a part id reappears with a different digest.

    Section 9 requires this to be refused as a contract conflict rather than
    silently overwritten: a shard whose content changed under the same name is
    not the same input, and replaying it would mix two different runs' data
    into one streak.
    """


class StateCompatibilityError(StorageError):
    """Raised when a stored alarm state cannot be used with the current rules.

    This is the deliberate failure direction of Issue #57 S3: when the rules
    changed under a state that carries history, consumption stops instead of
    quietly clearing the counters.
    """


@dataclass(frozen=True)
class WindowRecord:
    """One analysed window, ready to be written.

    Attributes:
        start_seq: Absolute sequence number of the window's first sample.
        end_seq_exclusive: One past the window's last sample.
        features: The feature record of the window.
        health: The health result of the window.
        alarms: The alarms raised by this window, if any.
        state: The alarm state after this window.
    """

    start_seq: int
    end_seq_exclusive: int
    features: FeatureRecord
    health: HealthResult
    alarms: tuple[Alarm, ...]
    state: AlarmState


def utc_now_text() -> str:
    """Return the current UTC instant as RFC 3339 text with a ``Z`` suffix.

    Returns:
        A timestamp such as ``2026-10-23T05:00:00.123456Z``.

    The format matches the manifest's ``start_time_utc`` spelling so the
    database holds one time format rather than two.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# state_json envelope
# --------------------------------------------------------------------------


def encode_state_json(
    state: AlarmState,
    *,
    reset_event: dict[str, Any] | None = None,
) -> str:
    """Encode an alarm state into the ``state_json`` envelope.

    Args:
        state: The state to store.
        reset_event: Audit record of an explicit reset, or ``None`` when the
            state was not reset.

    Returns:
        The JSON text to store.

    Raises:
        StorageError: if a counter is not a plain integer or a position is
            not an integer or ``None``. Encoding an impossible state here
            would only surface as an opaque failure on the next read.
    """
    payload: dict[str, Any] = {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "state": {
            "active_level": state.active_level,
            "consecutive_breaches": _as_int(
                state.consecutive_breaches, "consecutive_breaches"
            ),
            "normal_windows": _as_int(state.normal_windows, "normal_windows"),
            "alarm_windows": _as_int(state.alarm_windows, "alarm_windows"),
            "last_window_start_seq": _as_optional_int(
                state.last_window_start_seq, "last_window_start_seq"
            ),
            "recovered_at_seq": _as_optional_int(
                state.recovered_at_seq, "recovered_at_seq"
            ),
            "scoring_version": state.scoring_version,
            "rule_id": state.rule_id,
        },
        "reset_event": reset_event,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def decode_state_json(text: str) -> AlarmState:
    """Rebuild an alarm state from the ``state_json`` envelope.

    Args:
        text: The stored JSON text.

    Returns:
        The reconstructed state.

    Raises:
        StateCompatibilityError: if the envelope is malformed, carries an
            unknown version, has missing or unexpected keys, or holds a value
            of the wrong JSON type. Every one of those cases means the stored
            row cannot be trusted, and consuming it anyway would be guessing.
    """
    payload = _load_json(text, "state_json")

    if "state_schema_version" not in payload:
        raise StateCompatibilityError(
            "state_json has no state_schema_version; it was written before the "
            "field existed, so the rule set that produced it cannot be "
            "established. Start a new streak with AlarmState() if the old "
            "streak is to be dropped"
        )

    version = payload["state_schema_version"]
    if version != STATE_SCHEMA_VERSION:
        raise StateCompatibilityError(
            f"unsupported state_schema_version {version!r}; this consumer "
            f"understands {STATE_SCHEMA_VERSION!r}"
        )

    if "state" not in payload:
        raise StateCompatibilityError("state_json has no 'state' member")
    body = payload["state"]
    if not isinstance(body, dict):
        raise StateCompatibilityError("state_json's 'state' member is not an object")

    unknown = sorted(set(body) - set(_STATE_FIELDS))
    if unknown:
        raise StateCompatibilityError(
            f"state_json carries unknown state field(s) {unknown}; the "
            f"expected fields are {list(_STATE_FIELDS)}"
        )
    missing = sorted(set(_STATE_FIELDS) - set(body))
    if missing:
        raise StateCompatibilityError(
            f"state_json is missing state field(s) {missing}; the expected "
            f"fields are {list(_STATE_FIELDS)}"
        )

    values: dict[str, Any] = {}
    for field in _STATE_FIELDS:
        values[field] = _decode_state_value(field, body[field])

    return AlarmState(
        active_level=values["active_level"],
        consecutive_breaches=values["consecutive_breaches"],
        normal_windows=values["normal_windows"],
        alarm_windows=values["alarm_windows"],
        last_window_start_seq=values["last_window_start_seq"],
        recovered_at_seq=values["recovered_at_seq"],
        scoring_version=values["scoring_version"],
        rule_id=values["rule_id"],
    )


def _decode_state_value(field: str, value: Any) -> Any:
    """Return one decoded state field, refusing a value of the wrong type."""
    if field in _STATE_COUNTERS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise StateCompatibilityError(
                f"state_json's {field} must be a JSON integer, got {value!r}"
            )
        return value
    if field in _STATE_POSITIONS:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise StateCompatibilityError(
                f"state_json's {field} must be a JSON integer or null, got {value!r}"
            )
        return value
    if field == "active_level":
        if value is None or isinstance(value, str):
            return value
        raise StateCompatibilityError(
            f"state_json's active_level must be a string or null, got {value!r}"
        )
    # scoring_version / rule_id
    if value is None or isinstance(value, str):
        return value
    raise StateCompatibilityError(
        f"state_json's {field} must be a string or null, got {value!r}"
    )


# --------------------------------------------------------------------------
# features_json envelope
# --------------------------------------------------------------------------


def encode_features_json(
    features: FeatureRecord,
    health: HealthResult,
    *,
    window_start_seq: int,
) -> str:
    """Encode a window's features and score into the ``features_json`` envelope.

    Args:
        features: The feature record of the window.
        health: The health result of the window.
        window_start_seq: The window's position, stored for traceability.

    Returns:
        The JSON text to store.
    """
    payload: dict[str, Any] = {
        "features_version": FEATURES_SCHEMA_VERSION,
        "features": {name: float(getattr(features, name)) for name in FEATURE_NAMES},
        "sample_count": int(features.sample_count),
        "sample_rate_hz": int(features.sample_rate_hz),
        "window_start_seq": int(window_start_seq),
        "scoring_version": health.scoring_version,
        "rule_id": health.rule_id,
        "health_explain": health.explain(),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def decode_features_json(text: str) -> dict[str, Any]:
    """Read back the ``features_json`` envelope.

    Args:
        text: The stored JSON text.

    Returns:
        The decoded mapping.

    Raises:
        StorageError: if the envelope is malformed or carries an unknown
            version.
    """
    payload = _load_json(text, "features_json")
    version = payload.get("features_version")
    if version != FEATURES_SCHEMA_VERSION:
        raise StorageError(
            f"unsupported features_version {version!r}; this consumer "
            f"understands {FEATURES_SCHEMA_VERSION!r}"
        )
    return payload


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _load_json(text: str, label: str) -> dict[str, Any]:
    """Parse a JSON object, raising a storage error on anything else."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise StorageError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise StorageError(f"{label} must be a JSON object")
    return payload


def _as_int(value: Any, field: str) -> int:
    """Return ``value`` as a plain int, refusing bools and non-integers."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(f"AlarmState.{field} must be an int, got {value!r}")
    return int(value)


def _as_optional_int(value: Any, field: str) -> int | None:
    """Return ``value`` as an int or None, refusing bools and non-integers."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(f"AlarmState.{field} must be an int or None, got {value!r}")
    return int(value)


# --------------------------------------------------------------------------
# repository
# --------------------------------------------------------------------------


class ConsumptionRepository:
    """Reads and writes the consumer's rows in one SQLite database.

    Args:
        connection: An open connection whose schema has been initialised.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # -- runs -------------------------------------------------------------

    def ensure_run(
        self,
        run_id: str,
        scenario_id: str,
        schema_version: str,
        scoring_version: str,
        status: str,
        started_at: str,
    ) -> None:
        """Record the run being consumed, if it is not recorded yet.

        A repeated call is a no-op: the row for a run id is written once, and
        re-consuming the same run must not fail on the primary key.
        """
        with transaction(self._connection) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO runs
                  (run_id, scenario_id, schema_version, scoring_version,
                   status, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    scenario_id,
                    schema_version,
                    scoring_version,
                    status,
                    started_at,
                ),
            )

    # -- processed chunks -------------------------------------------------

    def get_processed_chunk_sha(self, run_id: str, part_id: int) -> str | None:
        """Return the digest recorded for a processed part, or ``None``.

        Args:
            run_id: The run identifier.
            part_id: The part identifier.

        Returns:
            The recorded SHA-256, or ``None`` when the part was never
            processed.
        """
        row = self._connection.execute(
            "SELECT sha256 FROM processed_chunks WHERE run_id = ? AND part_id = ?",
            (run_id, part_id),
        ).fetchone()
        return None if row is None else str(row[0])

    def mark_chunk_processed(
        self, run_id: str, part_id: int, sha256: str, processed_at: str
    ) -> None:
        """Record a part as processed. Must be called inside the shard transaction."""
        self._connection.execute(
            """
            INSERT INTO processed_chunks (run_id, part_id, sha256, processed_at)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, part_id, sha256, processed_at),
        )

    # -- feature windows --------------------------------------------------

    def insert_feature_window(
        self,
        run_id: str,
        device_id: str,
        start_seq: int,
        end_seq_exclusive: int,
        features_json: str,
        health_score: float,
    ) -> None:
        """Insert one analysed window. Must be called inside the shard transaction."""
        self._connection.execute(
            """
            INSERT INTO feature_windows
              (run_id, device_id, start_seq, end_seq_exclusive, features_json,
               health_score)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                device_id,
                start_seq,
                end_seq_exclusive,
                features_json,
                health_score,
            ),
        )

    def count_feature_windows(self, run_id: str | None = None) -> int:
        """Return the number of stored feature windows."""
        if run_id is None:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM feature_windows"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM feature_windows WHERE run_id = ?", (run_id,)
            ).fetchone()
        return 0 if row is None else int(row[0])

    # -- alarms -----------------------------------------------------------

    def insert_alarm(
        self,
        run_id: str,
        device_id: str,
        rule_id: str,
        start_seq: int,
        level: str,
        reason: str,
        created_at: str,
    ) -> None:
        """Insert one alarm. Must be called inside the shard transaction."""
        self._connection.execute(
            """
            INSERT INTO alarms
              (run_id, device_id, rule_id, start_seq, level, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, device_id, rule_id, start_seq, level, reason, created_at),
        )

    def count_alarms(self, run_id: str | None = None) -> int:
        """Return the number of stored alarms."""
        if run_id is None:
            row = self._connection.execute("SELECT COUNT(*) FROM alarms").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM alarms WHERE run_id = ?", (run_id,)
            ).fetchone()
        return 0 if row is None else int(row[0])

    # -- alarm states -----------------------------------------------------

    def get_alarm_state(
        self, run_id: str, device_id: str, rule_id: str
    ) -> AlarmState | None:
        """Return the stored alarm state, or ``None`` when there is none.

        Args:
            run_id: The run identifier.
            device_id: The device identifier.
            rule_id: The rule identifier.

        Returns:
            The rebuilt state, or ``None`` for a rule that has not produced a
            state yet.

        Raises:
            StateCompatibilityError: if the stored envelope cannot be read.
        """
        row = self._connection.execute(
            """
            SELECT state_json FROM alarm_states
            WHERE run_id = ? AND device_id = ? AND rule_id = ?
            """,
            (run_id, device_id, rule_id),
        ).fetchone()
        if row is None:
            return None
        return decode_state_json(str(row[0]))

    def get_last_end_seq_exclusive(
        self, run_id: str, device_id: str, rule_id: str
    ) -> int | None:
        """Return the recorded end position of the last committed window.

        Returns:
            The position, or ``None`` when no window has been committed for
            this rule yet.
        """
        row = self._connection.execute(
            """
            SELECT last_end_seq_exclusive FROM alarm_states
            WHERE run_id = ? AND device_id = ? AND rule_id = ?
            """,
            (run_id, device_id, rule_id),
        ).fetchone()
        return None if row is None else int(row[0])

    def upsert_alarm_state(
        self,
        run_id: str,
        device_id: str,
        rule_id: str,
        last_end_seq_exclusive: int,
        state_json: str,
    ) -> None:
        """Write the alarm state for a rule. Must be inside the shard transaction."""
        self._connection.execute(
            """
            INSERT INTO alarm_states
              (run_id, device_id, rule_id, last_end_seq_exclusive, state_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (run_id, device_id, rule_id) DO UPDATE SET
              last_end_seq_exclusive = excluded.last_end_seq_exclusive,
              state_json = excluded.state_json
            """,
            (run_id, device_id, rule_id, last_end_seq_exclusive, state_json),
        )

    # -- explicit reset (Issue #57 S3) ------------------------------------

    def reset_alarm_state(
        self,
        run_id: str,
        device_id: str,
        rule_id: str,
        *,
        reason: str,
        reset_at: str | None = None,
    ) -> AlarmState:
        """Archive the stored state and start a new streak.

        This is the explicit half of the Issue #57 S3 decision: consumption
        refuses an incompatible state, and an operator then calls this to clear
        it on purpose. The old state is kept verbatim in the envelope's
        ``reset_event`` so the reset is auditable.

        Args:
            run_id: The run identifier.
            device_id: The device identifier.
            rule_id: The rule identifier.
            reason: Why the streak is being reset. Stored in the audit record.
            reset_at: The reset timestamp; defaults to now.

        Returns:
            The new, empty state.

        Raises:
            StorageError: if there is no stored state to reset.
        """
        current = self._connection.execute(
            """
            SELECT state_json, last_end_seq_exclusive FROM alarm_states
            WHERE run_id = ? AND device_id = ? AND rule_id = ?
            """,
            (run_id, device_id, rule_id),
        ).fetchone()
        if current is None:
            raise StorageError(
                f"no alarm state is stored for run {run_id!r}, device "
                f"{device_id!r}, rule {rule_id!r}; there is nothing to reset"
            )

        previous = decode_state_json(str(current[0]))
        new_state = AlarmState(
            scoring_version=previous.scoring_version,
            rule_id=previous.rule_id,
        )
        reset_event = {
            "reason": reason,
            "reset_at_utc": reset_at or utc_now_text(),
            "previous_state": json.loads(str(current[0]))["state"],
        }
        with transaction(self._connection) as connection:
            connection.execute(
                """
                UPDATE alarm_states
                SET last_end_seq_exclusive = ?, state_json = ?
                WHERE run_id = ? AND device_id = ? AND rule_id = ?
                """,
                (
                    int(current[1]),
                    encode_state_json(new_state, reset_event=reset_event),
                    run_id,
                    device_id,
                    rule_id,
                ),
            )
        return new_state

    # -- introspection ----------------------------------------------------

    def get_connection(self) -> sqlite3.Connection:
        """Return the connection this repository writes to."""
        return self._connection

    def table_names(self) -> Sequence[str]:
        """Return the names of the tables in the database."""
        rows = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        return [str(row[0]) for row in rows]


def check_chunk_digest(recorded_sha: str | None, actual_sha: str, part_id: int) -> None:
    """Raise when a part's digest conflicts with what was recorded.

    Args:
        recorded_sha: The digest already stored for this part id, or
            ``None`` when the part was never processed.
        actual_sha: The digest the manifest declares for this part.
        part_id: The part identifier, for the error message.

    Raises:
        ChunkConflictError: if the part was processed with a different digest.
    """
    if recorded_sha is not None and recorded_sha != actual_sha:
        raise ChunkConflictError(
            f"part {part_id} was already processed with sha256 {recorded_sha} but "
            f"the manifest now declares {actual_sha}; refusing to replay a part "
            f"whose content changed"
        )


__all__.append("check_chunk_digest")
