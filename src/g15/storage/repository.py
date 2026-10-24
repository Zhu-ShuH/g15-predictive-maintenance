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
    looked like before (verbatim, even when the row is too old to decode). This
    is the storage layer's own area -- B2's dataclass does not know about it.
    Earlier resets are kept under ``reset_history``, and a routine window write
    re-attaches both, so an audit is never lost one window after it was
    written.

``features_json`` uses the same idea: a ``features_version``, the eight
feature values under their own names, the window's sample count and rate, the
window position, the rule identity, and a human-readable explanation of the
score.
"""

# One contract lives here on purpose: five tables, two JSON envelopes and the
# per-shard transaction rules are read together or not at all.
# pylint: disable=too-many-lines
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
    "RunConflictError",
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

#: The run-level fields that make up a run's identity. A change in any of them
#: under an existing ``run_id`` is refused rather than merged.
_RUN_IDENTITY_FIELDS: tuple[str, ...] = (
    "scenario_id",
    "schema_version",
    "scoring_version",
    "status",
    "started_at",
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


class RunConflictError(StorageError):
    """Raised when a run id reappears with different run-level metadata.

    A ``run_id`` is an identity, not a label: if the scenario, the scoring
    version or the start instant changed while the shard digests did not, the
    stored rows belong to a different analysis than the one being requested.
    Refusing is the same stance the consumer already takes for a shard whose
    digest changed under the same part id -- the alternative is a database that
    quietly mixes two rule sets under one run.
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
    reset_history: Sequence[dict[str, Any]] | None = None,
) -> str:
    """Encode an alarm state into the ``state_json`` envelope.

    Args:
        state: The state to store.
        reset_event: Audit record of an explicit reset, or ``None`` when the
            state was not reset.
        reset_history: Earlier reset records, oldest first. Written only when
            there is at least one, so a run that was never reset carries no
            empty list.

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
    if reset_history:
        payload["reset_history"] = list(reset_history)
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


def _refuse_changed_run(
    run_id: str, stored: dict[str, str], expected: dict[str, str]
) -> None:
    """Raise when a stored run row disagrees with the metadata being consumed.

    Args:
        run_id: The run identifier.
        stored: The metadata already recorded for that run.
        expected: The metadata of the manifest being consumed.

    Raises:
        RunConflictError: listing every field that differs.
    """
    differences = [
        f"{field}: stored {stored[field]!r}, requested {expected[field]!r}"
        for field in _RUN_IDENTITY_FIELDS
        if stored.get(field) != expected[field]
    ]
    if not differences:
        return
    raise RunConflictError(
        f"run {run_id!r} is already recorded with different metadata "
        f"({'; '.join(differences)}); refusing to mix two analyses under one "
        f"run id. Consume into a new database, or reset explicitly if the "
        f"change is intended"
    )


def _carry_reset_event(existing_text: str | None, new_text: str) -> str:
    """Re-attach a stored reset audit to a freshly encoded state.

    A normal window write encodes the state without a reset event, because the
    encoder knows nothing about history. Overwriting the whole envelope would
    therefore erase the audit of an explicit reset one window later -- the
    reset would still have happened, but nothing would say so. This helper
    keeps the audit in place for as long as the row exists.

    Args:
        existing_text: The ``state_json`` currently stored, if any.
        new_text: The freshly encoded envelope.

    Returns:
        ``new_text`` unchanged when there is nothing to carry, otherwise the
        same envelope with ``reset_event`` and ``reset_history`` restored.
    """
    if not existing_text:
        return new_text
    try:
        existing = json.loads(existing_text)
        fresh = json.loads(new_text)
    except (TypeError, ValueError):
        # A row that cannot be parsed is not something a write should repair;
        # the read path refuses it loudly instead.
        return new_text
    if not isinstance(existing, dict) or not isinstance(fresh, dict):
        return new_text

    carried = False
    if fresh.get("reset_event") is None and existing.get("reset_event"):
        fresh["reset_event"] = existing["reset_event"]
        carried = True
    if fresh.get("reset_history") is None and existing.get("reset_history"):
        fresh["reset_history"] = existing["reset_history"]
        carried = True
    if not carried:
        return new_text
    return json.dumps(fresh, ensure_ascii=False, sort_keys=True)


def _archived_state_body(text: str) -> tuple[dict[str, Any] | None, Any]:
    """Return the ``state`` body of an envelope and its declared version.

    This is the *archival* view: it deliberately does not validate. The
    operator's explicit reset must be able to archive a legacy or
    future-version row even though consumption would refuse it.

    Args:
        text: The stored ``state_json`` text.

    Returns:
        ``(body, version)``. ``body`` is ``None`` when the text carries no
        object under ``state``; ``version`` is whatever the row declares, or
        ``None`` for a legacy row written before the field existed.
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    body = payload.get("state")
    return (body if isinstance(body, dict) else None), payload.get(
        "state_schema_version"
    )


def _prior_reset_events(text: str) -> list[dict[str, Any]]:
    """Collect the reset records a row already carries, oldest first."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    history: list[dict[str, Any]] = []
    stored_history = payload.get("reset_history")
    if isinstance(stored_history, list):
        history.extend(item for item in stored_history if isinstance(item, dict))
    stored_event = payload.get("reset_event")
    if isinstance(stored_event, dict):
        history.append(stored_event)
    return history


def _text_or_none(value: Any) -> str | None:
    """Return ``value`` when it is a string, otherwise ``None``."""
    return value if isinstance(value, str) else None


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
        """Record the run being consumed, refusing a changed identity.

        A repeated call with the *same* metadata is a no-op: re-consuming a run
        must not fail on the primary key. A repeated call with *different*
        metadata is a contract conflict, because the stored windows were
        produced by another analysis than the one being requested.

        Args:
            run_id: The run identifier.
            scenario_id: The scenario the run came from.
            schema_version: The manifest schema version.
            scoring_version: The scoring version of the rules in use.
            status: The manifest status snapshot.
            started_at: The run's start instant.

        Raises:
            RunConflictError: if the run is already recorded with different
                scenario, schema, scoring version, status or start instant.
        """
        expected = {
            "scenario_id": scenario_id,
            "schema_version": schema_version,
            "scoring_version": scoring_version,
            "status": status,
            "started_at": started_at,
        }
        stored = self._read_run_row(run_id)
        if stored is not None:
            _refuse_changed_run(run_id, stored, expected)
            return

        try:
            with transaction(self._connection) as connection:
                connection.execute(
                    """
                    INSERT INTO runs
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
        except sqlite3.IntegrityError:
            # Another writer inserted the row between the check and the
            # insert. Re-read it and apply the same identity rule rather than
            # reporting a bare primary-key error.
            stored = self._read_run_row(run_id)
            if stored is None:
                raise
            _refuse_changed_run(run_id, stored, expected)

    def _read_run_row(self, run_id: str) -> dict[str, str] | None:
        """Return the stored run-level metadata, or ``None`` when absent."""
        row = self._connection.execute(
            """
            SELECT scenario_id, schema_version, scoring_version, status,
                   started_at
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(_RUN_IDENTITY_FIELDS, (str(value) for value in row)))

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

    def get_stored_window_points(self, run_id: str) -> int | None:
        """Return the window width the stored windows were cut at.

        The width is read back from the geometry of the first stored window
        (``end_seq_exclusive - start_seq``). It is the only trace of
        ``window_points`` section 9 leaves in the schema -- there is no column
        for it -- and it is enough to refuse a replay that would append windows
        cut on a different grid.

        Args:
            run_id: The run identifier.

        Returns:
            The stored window width, or ``None`` when nothing was written yet.
        """
        row = self._connection.execute(
            """
            SELECT end_seq_exclusive - start_seq FROM feature_windows
            WHERE run_id = ? ORDER BY start_seq LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return None if row is None else int(row[0])

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
        *,
        preserve_reset_event: bool = True,
    ) -> None:
        """Write the alarm state for a rule. Must be inside the shard transaction.

        Args:
            run_id: The run identifier.
            device_id: The device identifier.
            rule_id: The rule identifier.
            last_end_seq_exclusive: Position of the last committed window.
            state_json: The freshly encoded envelope.
            preserve_reset_event: Keep the audit of an earlier explicit reset.
                Defaults to ``True``: a routine window write must not erase the
                record that a reset happened (Issue #57 S1/S3).
        """
        payload = state_json
        if preserve_reset_event:
            payload = _carry_reset_event(
                self._read_state_json_text(run_id, device_id, rule_id), state_json
            )
        self._connection.execute(
            """
            INSERT INTO alarm_states
              (run_id, device_id, rule_id, last_end_seq_exclusive, state_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (run_id, device_id, rule_id) DO UPDATE SET
              last_end_seq_exclusive = excluded.last_end_seq_exclusive,
              state_json = excluded.state_json
            """,
            (run_id, device_id, rule_id, last_end_seq_exclusive, payload),
        )

    def _read_state_json_text(
        self, run_id: str, device_id: str, rule_id: str
    ) -> str | None:
        """Return the raw ``state_json`` text for a rule, or ``None``."""
        row = self._connection.execute(
            """
            SELECT state_json FROM alarm_states
            WHERE run_id = ? AND device_id = ? AND rule_id = ?
            """,
            (run_id, device_id, rule_id),
        ).fetchone()
        return None if row is None else str(row[0])

    # -- explicit reset (Issue #57 S3) ------------------------------------

    def reset_alarm_state(
        self,
        run_id: str,
        device_id: str,
        rule_id: str,
        *,
        reason: str,
        reset_at: str | None = None,
        scoring_version: str | None = None,
        allow_legacy: bool = True,
    ) -> AlarmState:
        """Archive the stored state and start a new streak.

        This is the explicit half of the Issue #57 S3 decision: consumption
        refuses an incompatible state, and an operator then calls this to clear
        it on purpose.

        The two paths are deliberately different:

        * **Consumption** decodes strictly. A row it cannot attribute to the
          current rules stops the run.
        * **This reset** is the operator's explicit act. It therefore does not
          need to *understand* the old row: it archives it verbatim and starts
          a new streak. Refusing to reset a legacy or future-version row would
          make the documented escape hatch unusable exactly when it is needed
          (Codex review of PR #64, R4).

        The audit lives in the envelope's ``reset_event``; earlier resets are
        kept under ``reset_history`` so repeated resets do not erase each
        other. ``upsert_alarm_state`` carries both forward, so a later routine
        window write cannot wipe the record.

        Args:
            run_id: The run identifier.
            device_id: The device identifier.
            rule_id: The rule identifier.
            reason: Why the streak is being reset. Stored in the audit record.
            reset_at: The reset timestamp; defaults to now.
            scoring_version: Identity to stamp on the new streak when the old
                row cannot be decoded. Defaults to the value found in the old
                row, if any.
            allow_legacy: Accept an old row the strict decoder refuses. Set to
                ``False`` to require a decodable current-version envelope.

        Returns:
            The new, empty state.

        Raises:
            StorageError: if there is no stored state to reset.
            StateCompatibilityError: if the row cannot be decoded and
                ``allow_legacy`` is ``False``.
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

        raw_text = str(current[0])
        archived_body, archived_version = _archived_state_body(raw_text)
        reset_event: dict[str, Any] = {
            "reason": reason,
            "reset_at_utc": reset_at or utc_now_text(),
            "previous_state_schema_version": archived_version,
            "previous_state_json": raw_text,
        }
        previous: AlarmState | None
        try:
            previous = decode_state_json(raw_text)
        except StateCompatibilityError:
            if not allow_legacy:
                raise
            previous = None

        if archived_body is not None:
            reset_event["previous_state"] = archived_body

        if previous is not None:
            identity_scoring = previous.scoring_version
            identity_rule = previous.rule_id
        else:
            identity_scoring = scoring_version or _text_or_none(
                (archived_body or {}).get("scoring_version")
            )
            identity_rule = (
                _text_or_none((archived_body or {}).get("rule_id")) or rule_id
            )

        new_state = AlarmState(
            scoring_version=identity_scoring,
            rule_id=identity_rule,
        )
        history = _prior_reset_events(raw_text)
        with transaction(self._connection) as connection:
            connection.execute(
                """
                UPDATE alarm_states
                SET last_end_seq_exclusive = ?, state_json = ?
                WHERE run_id = ? AND device_id = ? AND rule_id = ?
                """,
                (
                    int(current[1]),
                    encode_state_json(
                        new_state,
                        reset_event=reset_event,
                        reset_history=history or None,
                    ),
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
