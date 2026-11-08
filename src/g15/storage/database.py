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
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

__all__ = [
    "SCHEMA_STATEMENTS",
    "DatabaseError",
    "DatabaseReadOnlyError",
    "connect",
    "connect_readonly",
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


def connect(database_path: Path | str) -> sqlite3.Connection:
    """Open a database and apply the pragmas section 9 requires.

    The returned connection is ready to write: ``foreign_keys`` is on and WAL
    journalling is asserted. Creating the schema is a separate step
    (:func:`initialize_database`), so that *opening* a database and *preparing*
    a database for writes stay distinguishable.

    Args:
        database_path: Path to the SQLite file. Parent directories are created
            if they do not exist.

    Returns:
        An open connection with ``foreign_keys`` on and WAL journalling
        asserted.

    Raises:
        DatabaseError: if the database cannot be opened or the pragmas cannot
            be applied.
        DatabaseReadOnlyError: if the write that asserts WAL journalling was
            refused with ``SQLITE_READONLY``. Other operational failures are
            reported as :class:`DatabaseError` with their original context.
    """
    path = Path(database_path)
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
#: ``sqlite3.Error.sqlite_errorcode`` reports the *extended* code (for example
#: ``SQLITE_READONLY_ROLLBACK``), so the low byte identifies the primary one.
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


def _write_error(
    path: Path | None, exc: sqlite3.OperationalError, action: str
) -> DatabaseError:
    """Return the error that names a failed write without guessing too much.

    Only ``SQLITE_READONLY`` (primary code 8) means "read-only". Anything else
    -- ``SQLITE_ERROR`` for a missing table, ``SQLITE_BUSY`` for a lock
    conflict, ``SQLITE_NOTADB`` for a corrupt file -- keeps its own meaning and
    is reported as a plain :class:`DatabaseError` carrying the original
    context. Classifying by code rather than by wording is what keeps
    "another connection holds it" from being blamed on an unrelated bug.

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
    )


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
        raise _write_error(
            _database_path(connection), exc, "initialise the database schema"
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
