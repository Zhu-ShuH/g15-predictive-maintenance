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
    """Raised when a write was refused because the database was read-only.

    SQLite reports this as a single ``SQLITE_READONLY`` code that covers two
    situations a caller must be able to tell apart:

    * another connection holds the database, and the write was refused while
      that connection was changing the journal mode;
    * the path itself cannot be written to.

    The two need different responses -- the first is a contention problem that
    a retry or a read-only connection resolves, the second is a permissions or
    deployment problem that no retry resolves. They are therefore raised as
    this dedicated type, and the message says which one it was (Issue #101,
    completion condition 4).
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
            refused because the database was read-only.
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
        raise _readonly_error(path, exc) from exc
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


def _readonly_error(path: Path, exc: sqlite3.OperationalError) -> DatabaseError:
    """Return the error that names why a write was refused as read-only.

    SQLite reports "attempt to write a readonly database" for two different
    situations, and they need different answers: another connection holding
    the file is contention, while a path that cannot be written is a
    deployment problem. The path's own writability tells them apart.

    Args:
        path: The database the write was refused on.
        exc: The error SQLite raised.

    Returns:
        A :class:`DatabaseReadOnlyError` naming the cause.
    """
    if not os.access(path, os.W_OK):
        return DatabaseReadOnlyError(
            f"cannot write database {path}: the path is not writable by this "
            f"process ({exc})"
        )
    return DatabaseReadOnlyError(
        f"cannot write database {path}: another connection is holding the "
        f"database and refused the write ({exc})"
    )


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the section 9 tables if they do not exist yet.

    Args:
        connection: An open connection.

    Raises:
        DatabaseError: if the schema cannot be created.
        DatabaseReadOnlyError: if the write was refused because the database
            was read-only, either because another connection holds it or
            because the path is not writable.
    """
    try:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
    except sqlite3.OperationalError as exc:
        path = _database_path(connection)
        if path is not None:
            raise _readonly_error(path, exc) from exc
        raise DatabaseError(f"cannot initialise the database schema: {exc}") from exc
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
        DatabaseError: if the transaction cannot be started or committed.
        DatabaseReadOnlyError: if a write inside the block was refused
            because the database was read-only. The cause is named, so that
            "another connection holds it" is not confused with "the path is
            not writable" (Issue #101, completion condition 4).
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        path = _database_path(connection)
        if path is not None:
            raise _readonly_error(path, exc) from exc
        raise DatabaseError(f"cannot start a transaction: {exc}") from exc
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot start a transaction: {exc}") from exc

    try:
        yield connection
    except sqlite3.OperationalError as exc:
        _rollback(connection)
        path = _database_path(connection)
        if path is not None:
            raise _readonly_error(path, exc) from exc
        raise DatabaseError(f"cannot write to the database: {exc}") from exc
    except Exception:
        _rollback(connection)
        raise

    try:
        connection.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        path = _database_path(connection)
        if path is not None:
            raise _readonly_error(path, exc) from exc
        raise DatabaseError(f"cannot commit the transaction: {exc}") from exc
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
