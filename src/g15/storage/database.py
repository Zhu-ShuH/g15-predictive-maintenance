"""SQLite storage for the consumer (Issue #8, owner C2).

This module owns the *shape* of the database: the five tables of
``docs/interface.md`` section 9, the connection pragmas, and the transaction
helper the repository uses. It deliberately owns no analysis logic and no
knowledge of the alarm rules -- it only knows how to open a database, create
the schema, and hand out transactions.

Three properties of section 9 are implemented here because they are properties
of the *database*, not of any one call site:

``PRAGMA foreign_keys=ON``
    Off by default in SQLite, so it is set on every connection and verified.

``PRAGMA journal_mode=WAL``
    Required so a reader is not blocked by the writer, and so an interrupted
    write leaves the database consistent. The pragma is persistent, but it is
    re-asserted and read back so a database moved between machines cannot
    silently run in rollback-journal mode.

One transaction per shard
    :func:`transaction` is the only way the repository writes. Features, scores,
    alarms, the alarm state and the chunk marker are written inside a single
    ``with connection:`` block, so a crash can never leave a chunk marked as
    processed while its features are missing.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

__all__ = [
    "SCHEMA_STATEMENTS",
    "DatabaseError",
    "DatabaseReadOnlyError",
    "connect",
    "connect_readonly",
    "describe_read_only_context",
    "initialize_database",
    "transaction",
]


class DatabaseError(Exception):
    """Raised when the database cannot be opened or initialised.

    This is the storage-layer error the consumer maps onto the CLI exit code
    for I/O and schema failures (exit code 3 in ``docs/interface.md`` section
    10).
    """


class DatabaseReadOnlyError(DatabaseError):
    """Raised when a write was refused with ``SQLITE_READONLY``.

    This type is reserved for that one primary result code (8). Every other
    ``OperationalError`` -- a missing table, a lock conflict, a corrupt file --
    keeps its own meaning and is raised as plain :class:`DatabaseError`; folding
    them into "the database is read-only" points the reader at contention when
    the real cause may be a typo in a table name (see the review of #118).

    Within ``SQLITE_READONLY`` itself, two situations want different responses:

    * another connection holds the database and the write was refused;
    * the path itself cannot be written to -- a permissions or deployment
      problem, where a retry never helps.

    The path's own writability suggests which is more likely, but it is a
    heuristic, not a determination: a writable path can still be held by
    another process, and a read-only connection with no second holder reports
    the same code. The message therefore says "most likely" instead of
    asserting a cause (Issue #101, completion condition 4).
    """


#: The five tables of ``docs/interface.md`` section 9, in the order they are
#: created. The column lists are taken verbatim from the section 9 SQL; the
#: only additions are ``IF NOT EXISTS`` (so re-initialising an existing
#: database is a no-op) and two indexes, which section 9 does not forbid -- it
#: says the core tables' fields may be extended and that the primary and
#: foreign key semantics must be tested.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY,
      scenario_id TEXT NOT NULL,
      schema_version TEXT NOT NULL,
      scoring_version TEXT NOT NULL,
      status TEXT NOT NULL,
      started_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_chunks (
      run_id TEXT NOT NULL,
      part_id INTEGER NOT NULL,
      sha256 TEXT NOT NULL,
      processed_at TEXT NOT NULL,
      PRIMARY KEY (run_id, part_id),
      FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feature_windows (
      run_id TEXT NOT NULL,
      device_id TEXT NOT NULL,
      start_seq INTEGER NOT NULL,
      end_seq_exclusive INTEGER NOT NULL,
      features_json TEXT NOT NULL,
      health_score REAL NOT NULL CHECK(health_score BETWEEN 0 AND 100),
      PRIMARY KEY (run_id, device_id, start_seq),
      FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alarms (
      run_id TEXT NOT NULL,
      device_id TEXT NOT NULL,
      rule_id TEXT NOT NULL,
      start_seq INTEGER NOT NULL,
      level TEXT NOT NULL,
      reason TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (run_id, device_id, rule_id, start_seq),
      FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alarm_states (
      run_id TEXT NOT NULL,
      device_id TEXT NOT NULL,
      rule_id TEXT NOT NULL,
      last_end_seq_exclusive INTEGER NOT NULL,
      state_json TEXT NOT NULL,
      PRIMARY KEY (run_id, device_id, rule_id),
      FOREIGN KEY (run_id) REFERENCES runs(run_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_feature_windows_run
      ON feature_windows (run_id, start_seq)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_alarms_run
      ON alarms (run_id, start_seq)
    """,
)


#: How many times :func:`connect` re-opens the database when the WAL handle
#: came back degraded (see :func:`_wal_handle_is_read_only`).
#:
#: Each attempt re-opens the file, which re-runs SQLite's own open logic and
#: therefore re-decides whether the WAL handle is writable.  The retry is
#: bounded so that a database that is *permanently* held by another process
#: fails with a named error instead of spinning.
_CONNECT_ATTEMPTS = 12

#: How long :func:`connect` waits between two attempts, in seconds.
#:
#: The wait is not the fix: detecting the degraded handle and re-opening is.
#: It only gives the process that holds the file the moment it needs to let
#: go, which is what makes the retry succeed in the common case where the
#: holder is a short-lived reader.  The default window covers a reader that
#: holds the files for a few hundred milliseconds.
_CONNECT_RETRY_DELAY = 0.05


def connect(database_path: Path | str) -> sqlite3.Connection:
    """Open a database and apply the pragmas section 9 requires.

    The returned connection is ready to write: ``foreign_keys`` is on and WAL
    journalling is asserted. Creating the schema is a separate step
    (:func:`initialize_database`), so that *opening* a database and *preparing*
    a database for writes stay distinguishable.

    The returned handle is also **verified to be writable**.  On Windows,
    SQLite's VFS silently downgrades a read/write open of the ``-wal`` and
    ``-shm`` files to a read-only one when another process holds them without
    write sharing, and it caches that decision on the WAL handle.  A writer
    that received such a handle fails later -- at the first real write, with
    a bare ``SQLITE_READONLY`` -- which is Issue #123.  Detecting the
    degraded handle here, while the connection is still cheap to replace,
    keeps that failure from reaching the consumer.

    Args:
        database_path: Path to the SQLite file. Parent directories are created
            if they do not exist.

    Returns:
        An open connection with ``foreign_keys`` on, WAL journalling
        asserted, and a WAL handle that can actually write.

    Raises:
        DatabaseError: if the database cannot be opened or the pragmas cannot
            be applied.
        DatabaseReadOnlyError: if the write that asserts WAL journalling was
            refused with ``SQLITE_READONLY``. Other operational failures are
            reported as :class:`DatabaseError` with their original context.
    """
    path = Path(database_path)
    last_error: sqlite3.Error | None = None

    for attempt in range(1, _CONNECT_ATTEMPTS + 1):
        try:
            connection = _open_and_configure(path)
        except DatabaseReadOnlyError as exc:
            last_error = (
                exc.__cause__ if isinstance(exc.__cause__, sqlite3.Error) else None
            )
            if attempt == _CONNECT_ATTEMPTS:
                raise
            time.sleep(_CONNECT_RETRY_DELAY)
            continue
        try:
            degraded = _wal_handle_is_read_only(connection)
        except _ProbeRollbackError as exc:
            # The probe could not roll its transaction back, so the connection
            # is still inside a transaction nobody asked for.  Handing it out
            # would break the caller's next ``BEGIN``; close it and report
            # the failure with its original context.
            connection.close()
            cause = exc.__cause__ if isinstance(exc.__cause__, sqlite3.Error) else None
            raise DatabaseError(
                f"cannot open database {path}: the write-capability probe could "
                f"not be rolled back"
                f"{f' ({cause})' if cause is not None else ''}"
            ) from cause
        if degraded:
            if not os.access(path, os.W_OK):
                # The path itself is read-only. That is a permission problem,
                # not a degraded handle, and it is reported when the write
                # happens -- naming it here would change the contract of
                # :func:`connect`, which opens databases it cannot write to
                # so that the refusal can be named at the point of the write.
                return connection
            # The handle was degraded while it was being opened.  Closing and
            # re-opening gives SQLite a fresh chance at the same files, and
            # the check runs again on the new handle.
            connection.close()
            if attempt < _CONNECT_ATTEMPTS:
                time.sleep(_CONNECT_RETRY_DELAY)
            continue
        return connection
    # Every attempt produced a degraded handle.  Report it as the read-only
    # refusal it is, with the diagnostics that say why.
    raise _write_error(
        path,
        last_error if last_error is not None else _readonly_probe_error(),
        f"open database {path} for writing",
    )


def _readonly_probe_error() -> sqlite3.Error:
    """Build the error used when a degraded WAL handle was never recovered."""
    error = sqlite3.OperationalError("attempt to write a readonly database")
    # The real error carries this; a synthesised one has to say so too, or
    # the classifier cannot tell it apart from any other operational error.
    error.sqlite_errorcode = _SQLITE_READONLY  # type: ignore[attr-defined]
    error.sqlite_errorname = "SQLITE_READONLY"  # type: ignore[attr-defined]
    return error


def _open_and_configure(path: Path) -> sqlite3.Connection:
    """Open the database and apply the pragmas section 9 requires.

    Args:
        path: Path to the SQLite file. Parent directories are created if they
            do not exist.

    Returns:
        An open connection with the pragmas applied.

    Raises:
        DatabaseError: if the database cannot be opened or the pragmas cannot
            be applied.
        DatabaseReadOnlyError: if a write was refused with
            ``SQLITE_READONLY``.
    """
    try:
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), isolation_level=None)
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError(f"cannot open database {path}: {exc}") from exc

    try:
        # ``foreign_keys`` is a no-op inside a transaction, so it must be set
        # before any write. ``isolation_level=None`` puts the connection in
        # autocommit mode, which is what lets the explicit BEGIN below work.
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")

        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise DatabaseError(
                f"cannot enable foreign keys on {path}; section 9 requires it"
            )
        journal = connection.execute("PRAGMA journal_mode").fetchone()
        if journal is None or str(journal[0]).lower() != "wal":
            raise DatabaseError(
                f"cannot enable WAL journalling on {path}; section 9 requires it"
            )
    except sqlite3.OperationalError as exc:
        connection.close()
        raise _write_error(path, exc, f"configure database {path}") from exc
    except sqlite3.Error as exc:
        connection.close()
        raise DatabaseError(f"cannot configure database {path}: {exc}") from exc

    return connection


def _wal_handle_is_read_only(connection: sqlite3.Connection) -> bool:
    """Report whether this connection's WAL handle cannot write.

    SQLite exposes no direct "is the WAL handle read-only" flag, but it does
    expose the consequence of one: a WAL handle that was opened read-only
    refuses every write transaction with a bare ``SQLITE_READONLY`` (primary
    code 8, extended code 0).  Probing that with a transaction that changes
    nothing is therefore the reliable way to ask.

    The probe is a ``BEGIN IMMEDIATE`` followed by a ``ROLLBACK``: it takes
    the write lock and gives it straight back, so it writes nothing and
    leaves the database exactly as it found it.

    The probe runs with ``busy_timeout = 0`` so that a *concurrent writer*
    cannot make it wait: ``BEGIN IMMEDIATE`` would otherwise block for the
    connection's busy timeout (5 seconds by default) before SQLite reports
    ``SQLITE_BUSY``.  That wait buys nothing -- ``SQLITE_BUSY`` is lock
    contention, not a degraded handle, so the probe would answer "writable"
    anyway after paying five seconds for it.  With the timeout at zero the
    contention is reported immediately and the detection is unchanged.

    Args:
        connection: An open connection.

    Returns:
        ``True`` when the connection cannot start a write transaction.

    Raises:
        _ProbeRollbackError: if the probe transaction could not be rolled
            back.  The connection is then in an unknown transaction state and
            must not be handed to a caller.
    """
    previous_timeout = _probe_timeout(connection, 0)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # Only a read-only refusal means "the handle is degraded".  A busy
            # database or a locked file is contention, and must not be reported
            # as a degraded handle.
            return _primary_error_code(exc) == _SQLITE_READONLY
        except sqlite3.Error:
            return False
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error as exc:
            # The transaction was started, so the handle *is* writable; failing
            # to roll it back is a different problem and not this one.  But the
            # connection is still inside the probe transaction, so it must not
            # be returned to a caller: report the failure instead.
            raise _ProbeRollbackError(exc) from exc
        return False
    finally:
        _restore_timeout(connection, previous_timeout)


class _ProbeRollbackError(DatabaseError):
    """Raised when the write-capability probe cannot roll its transaction back.

    The probe opens a transaction to test whether the handle can write.  If
    the matching ``ROLLBACK`` fails, the connection is left inside a
    transaction nobody asked for, and a later ``BEGIN`` on it fails with
    "cannot start a transaction within a transaction".  Returning such a
    connection would hand the caller a broken handle, so the failure is
    reported instead of being swallowed.
    """


def _probe_timeout(connection: sqlite3.Connection, timeout_ms: int) -> int:
    """Set the busy timeout for a probe and return the previous value.

    Args:
        connection: The connection to configure.
        timeout_ms: The busy timeout to apply, in milliseconds.

    Returns:
        The busy timeout that was in effect before, or ``0`` when it could not
        be read.
    """
    previous = 0
    try:
        row = connection.execute("PRAGMA busy_timeout").fetchone()
        if row is not None:
            previous = int(row[0])
    except (sqlite3.Error, TypeError, ValueError, IndexError):
        previous = 0
    try:
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    except sqlite3.Error:  # pragma: no cover - the pragma is optional
        pass
    return previous


def _restore_timeout(connection: sqlite3.Connection, timeout_ms: int) -> None:
    """Restore a busy timeout that :func:`_probe_timeout` changed.

    Args:
        connection: The connection to configure.
        timeout_ms: The busy timeout to restore, in milliseconds.
    """
    try:
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    except sqlite3.Error:  # pragma: no cover - the pragma is optional
        pass


def connect_readonly(database_path: Path | str) -> sqlite3.Connection:
    """Open an existing database without the ability to write to it.

    This is the read-only counterpart of :func:`connect`, and it exists
    because a plain open is **not** read-only: ``sqlite3.connect`` creates the
    file when it is missing, and ``PRAGMA journal_mode = WAL`` rewrites the
    database header. An observer that opens a database the way
    :func:`connect` does therefore *writes* to it, and two processes doing that
    to the same file can make the writer fail with ``SQLITE_READONLY``.

    A read-only connection cannot do either, so it never creates the file,
    never changes the journal mode, and never competes for the write lock.

    Args:
        database_path: Path to an **existing** SQLite file.

    Returns:
        An open read-only connection.

    Raises:
        DatabaseError: if the file does not exist or cannot be opened.
    """
    path = Path(database_path)
    if not path.is_file():
        raise DatabaseError(f"no database at {path}; it cannot be opened read-only")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError(f"cannot open database {path} read-only: {exc}") from exc
    return connection


#: Primary SQLite result code for "attempt to write a readonly database".
#: ``sqlite3.Error.sqlite_errorcode`` reports the *full* result code -- the
#: primary code in the low byte and the extended code in the upper bits -- so
#: the low byte identifies the primary one.
_SQLITE_READONLY = 8


def _primary_error_code(exc: sqlite3.Error) -> int | None:
    """Return the primary result code behind a SQLite error, if any.

    Args:
        exc: The error SQLite raised.

    Returns:
        The primary result code (``sqlite_errorcode & 0xFF``), or ``None`` when
        the exception does not carry one.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if code is None:
        return None
    return int(code) & 0xFF


def _extended_error_code(exc: sqlite3.Error) -> int | None:
    """Return the extended result code behind a SQLite error, if any.

    ``sqlite3.Error.sqlite_errorcode`` carries the **full** result code: the
    primary code in the low byte and the extended code in the upper bits.  The
    extended code is therefore the full code shifted down by eight bits, and
    it is what separates the flavours of a read-only refusal from each other:
    a bare ``SQLITE_READONLY`` is primary 8 / extended 0, while
    ``SQLITE_READONLY_CANTINIT`` is primary 8 / extended 3.

    Args:
        exc: The error SQLite raised.

    Returns:
        The extended result code (``sqlite_errorcode >> 8``), or ``None`` when
        the exception does not carry one.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if code is None:
        return None
    return int(code) >> 8


def _write_error(
    path: Path | None,
    exc: sqlite3.Error,
    action: str,
    connection: sqlite3.Connection | None = None,
) -> DatabaseError:
    """Return the error that names a failed write without guessing too much.

    Only ``SQLITE_READONLY`` (primary code 8) means "read-only". Anything else
    -- ``SQLITE_ERROR`` for a missing table, ``SQLITE_BUSY`` for a lock
    conflict, ``SQLITE_NOTADB`` for a corrupt file -- keeps its own meaning and
    is reported as a plain :class:`DatabaseError` carrying the original
    context. Classifying by code rather than by wording is what keeps
    "another connection holds it" from being blamed on an unrelated bug.

    The message carries the process-level context that Issue #123 asked for,
    because a bare ``SQLITE_READONLY`` on its own says only that *a* write
    was refused and not *what* refused it.

    Args:
        path: The database behind the connection, or ``None`` if unknown.
        exc: The error SQLite raised.
        action: What the caller was trying to do, used in the message.

    Returns:
        A :class:`DatabaseReadOnlyError` when the code is ``SQLITE_READONLY``,
        otherwise a :class:`DatabaseError`.
    """
    if _primary_error_code(exc) != _SQLITE_READONLY:
        return DatabaseError(f"cannot {action}: {exc}")
    if path is not None and not os.access(path, os.W_OK):
        return DatabaseReadOnlyError(
            f"cannot {action}: {path} is not writable by this process ({exc})"
        )
    return DatabaseReadOnlyError(
        f"cannot {action}: another connection most likely holds {path} and "
        f"refused the write -- that is the likeliest cause when the path "
        f"itself is writable, not a determination ({exc})"
        f"{_diagnostic_suffix(path, exc, connection)}"
    )


def describe_read_only_context(
    path: Path | str | None,
    exc: sqlite3.Error | None = None,
    connection: sqlite3.Connection | None = None,
) -> str:
    """Return the process-level context of a refused write.

    Issue #123 asked for the diagnostics that turn "the write was refused"
    into something a reviewer can check: the SQLite primary and extended
    codes, the absolute path, the journal mode, and the state of the
    ``-wal`` and ``-shm`` files that WAL mode adds next to the database.

    Args:
        path: The database path, or ``None`` if unknown.
        exc: The SQLite error, when there is one.
        connection: The connection the write failed on, when it is still
            open. It is used to read the journal mode back, because the
            journal mode is a property of the *connection* to the database
            and not of the file.

    Returns:
        A single-line summary, or an empty string when there is nothing to
        report.
    """
    parts: list[str] = []
    if exc is not None:
        primary = _primary_error_code(exc)
        extended = _extended_error_code(exc)
        name = getattr(exc, "sqlite_errorname", None)
        parts.append(f"sqlite primary={primary} extended={extended} name={name}")
    if connection is not None:
        parts.append(f"journal_mode={_journal_mode(connection)}")
    if path is None:
        return "; ".join(parts)
    resolved = Path(path).resolve()
    parts.append(f"database={resolved}")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(resolved) + suffix)
        if sidecar.exists():
            try:
                size = sidecar.stat().st_size
            except OSError:  # pragma: no cover - the file vanished meanwhile
                size = -1
            parts.append(f"{suffix}={size}B")
        else:
            parts.append(f"{suffix}=absent")
    return "; ".join(parts)


def _journal_mode(connection: sqlite3.Connection) -> str:
    """Return the journal mode of a connection, or ``unknown``."""
    try:
        row = connection.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.Error:  # pragma: no cover - the connection is broken
        return "unknown"
    if row is None:
        return "unknown"
    return str(row[0])


def _diagnostic_suffix(
    path: Path | None, exc: sqlite3.Error, connection: sqlite3.Connection | None = None
) -> str:
    """Return the diagnostic context appended to a read-only refusal."""
    context = describe_read_only_context(path, exc, connection)
    if not context:
        return ""
    return f" [{context}]"


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the section 9 tables if they do not exist yet.

    Args:
        connection: An open connection.

    Raises:
        DatabaseError: if the schema cannot be created.
        DatabaseReadOnlyError: if the write was refused with
            ``SQLITE_READONLY`` -- another connection most likely holds the
            database, or the path itself is not writable. Other operational
            failures keep their own meaning and are raised as
            :class:`DatabaseError`.
    """
    try:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
    except sqlite3.OperationalError as exc:
        path = _database_path(connection)
        raise _write_error(
            path, exc, "initialise the database schema", connection
        ) from exc
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot initialise the database schema: {exc}") from exc


def _database_path(connection: sqlite3.Connection) -> Path | None:
    """Return the file behind a connection, or ``None`` for an in-memory one."""
    try:
        row = connection.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    if row is None or len(row) < 3:
        return None
    name = str(row[2])
    return Path(name) if name else None


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one transaction that is committed as a unit.

    The block either completes -- and every write inside it becomes visible
    together -- or it raises, and SQLite rolls the whole thing back. This is
    the mechanism behind the section 9 requirement that features, scores,
    alarms and the chunk marker share a single transaction.

    Args:
        connection: An open connection in autocommit mode.

    Yields:
        The same connection, for statements to be issued against.

    Raises:
        DatabaseError: if the transaction cannot be started or committed, or
            if a statement inside the block fails. A ``SQLITE_READONLY``
            failure is raised as the subclass below; every other operational
            failure keeps its own meaning instead of being reported as
            read-only.
        DatabaseReadOnlyError: if a write was refused with
            ``SQLITE_READONLY``. The most likely cause is named, so that
            "another connection most likely holds it" is not confused with
            "the path is not writable" (Issue #101, completion condition 4).
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise _write_error(
            _database_path(connection), exc, "start a transaction"
        ) from exc
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot start a transaction: {exc}") from exc

    try:
        yield connection
    except sqlite3.OperationalError as exc:
        _rollback(connection)
        raise _write_error(
            _database_path(connection), exc, "write to the database"
        ) from exc
    except Exception:
        _rollback(connection)
        raise

    try:
        connection.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        raise _write_error(
            _database_path(connection), exc, "commit the transaction"
        ) from exc
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot commit the transaction: {exc}") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    """Roll a transaction back, ignoring a rollback that cannot happen."""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        # The original error is the one worth reporting; a rollback that
        # fails because the connection is already broken adds nothing.
        pass
