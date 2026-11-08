"""Storage-layer tests: the connection contract and the #101 race guard.

The consumer tests in ``tests/unit/test_consumer.py`` cover the *behaviour*
the consumer produces. This module covers the *storage layer's own*
contract, which is where Issue #101 landed:

* opening a database and preparing it for writes are separate steps;
* a read-only observation never writes, so it cannot make a writer fail;
* a refused write names the reason instead of reporting one blanket error.

The guard test at the end is the one that matters most: it is the two-process
case Issue #101 asks for -- one process creates the schema while another
observes the same file -- and it is a regression test, so a change that
reintroduces the race fails here rather than in a flaky stress round.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import time
from pathlib import Path

import pytest

from g15.storage.database import (
    SCHEMA_STATEMENTS,
    DatabaseError,
    DatabaseReadOnlyError,
    connect,
    connect_readonly,
    initialize_database,
    transaction,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _child_writes(database: str, ready, stop) -> None:  # type: ignore[no-untyped-def]
    """Create the schema, then keep writing, like the consumer does."""
    connection = connect(Path(database))
    try:
        initialize_database(connection)
        ready.set()
        while not stop.is_set():
            connection.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                ("r", "s", "2.0", "v", "complete", "2026-01-01T00:00:00Z"),
            )
            time.sleep(0.002)
    finally:
        connection.close()


def _child_observes(database: str, successes, stop) -> None:  # type: ignore[no-untyped-def]
    """Observe the database read-only, and report how often that worked.

    The connection is opened as soon as the file exists and then *held*: an
    idle read-only connection holds no locks, so the writer can proceed, while
    the observer still overlaps the schema-creation window. Errors are
    expected until the schema exists, so they are retried on the same handle.
    The count of successful reads goes back to the parent, because "the
    observer never failed" is only meaningful if it actually read something.
    """
    reads = 0
    connection = None
    while not stop.is_set():
        if connection is None:
            try:
                connection = connect_readonly(Path(database))
            except DatabaseError:
                # The database does not exist yet; that is expected and is not
                # an error for an observer.
                time.sleep(0.002)
                continue
        try:
            connection.execute("SELECT COUNT(*) FROM processed_chunks").fetchone()
            reads += 1
        except sqlite3.Error:
            # The schema may not exist yet; keep polling on the same handle.
            pass
        time.sleep(0.002)
    if connection is not None:
        connection.close()
    successes.put(reads)


# --------------------------------------------------------------------------
# the read-only connection
# --------------------------------------------------------------------------


def test_a_read_only_connection_does_not_create_the_database(tmp_path: Path) -> None:
    """Observing a database that does not exist must not bring it into being.

    ``sqlite3.connect`` creates the file it is given. An observer that opens a
    database the write path opens therefore creates it, and that creation is
    the write that can make the real writer fail (Issue #101).
    """
    database = tmp_path / "absent.db"

    with pytest.raises(DatabaseError):
        connect_readonly(database)

    assert not database.exists(), "a read-only connection created the database"


def test_a_read_only_connection_can_read_a_database(tmp_path: Path) -> None:
    """The read-only connection sees the same rows the writer committed."""
    database = tmp_path / "g15.db"
    connection = connect(database)
    try:
        initialize_database(connection)
        connection.execute(
            "INSERT INTO runs VALUES ('r','s','2.0','v','complete','2026-01-01')"
        )
    finally:
        connection.close()

    readonly = connect_readonly(database)
    try:
        count = readonly.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        readonly.close()

    assert int(count) == 1


def test_a_read_only_connection_cannot_write(tmp_path: Path) -> None:
    """The read-only connection is genuinely read-only."""
    database = tmp_path / "g15.db"
    connection = connect(database)
    try:
        initialize_database(connection)
    finally:
        connection.close()

    readonly = connect_readonly(database)
    try:
        with pytest.raises(sqlite3.Error):
            readonly.execute("INSERT INTO runs VALUES ('x','s','2.0','v','c','t')")
    finally:
        readonly.close()


# --------------------------------------------------------------------------
# the named read-only error
# --------------------------------------------------------------------------


def test_a_refused_write_names_the_reason(tmp_path: Path) -> None:
    """A refused write says the path is unwritable, not "cannot configure".

    SQLite reports "attempt to write a readonly database" for a path it cannot
    write to. The storage layer must name that cause, because the alternative
    -- folding it into one blanket configuration error -- is what made Issue
    #101 hard to diagnose.
    """
    database = tmp_path / "g15.db"
    connection = connect(database)
    try:
        initialize_database(connection)
    finally:
        connection.close()

    if os.name != "nt":
        pytest.skip("the read-only file attribute is enforced differently")
    os.chmod(database, 0o444)
    try:
        connection = connect(database)
        try:
            with pytest.raises(DatabaseReadOnlyError) as caught:
                with transaction(connection):
                    connection.execute(
                        "INSERT INTO runs VALUES ('r','s','2.0','v','c','2026-01-01')"
                    )
        finally:
            connection.close()
    finally:
        os.chmod(database, 0o644)

    assert "not writable" in str(caught.value)


def test_a_missing_table_is_not_reported_as_read_only(tmp_path: Path) -> None:
    """A plain SQL error keeps its own meaning instead of becoming read-only.

    ``SQLITE_ERROR`` (code 1) inside a transaction means the statement was
    wrong -- a misspelt table here. Reporting it as "another connection most
    likely holds the database" sends the reader hunting for concurrency bugs.
    """
    database = tmp_path / "g15.db"
    connection = connect(database)
    try:
        initialize_database(connection)
        with pytest.raises(DatabaseError) as caught:
            with transaction(connection):
                connection.execute("SELECT * FROM no_such_table").fetchall()
    finally:
        connection.close()

    assert not isinstance(caught.value, DatabaseReadOnlyError)
    assert "no_such_table" in str(caught.value)


def test_a_locked_database_is_not_reported_as_read_only(tmp_path: Path) -> None:
    """``SQLITE_BUSY`` is contention, not a read-only database.

    A held write lock makes the next writer fail with code 5. The two need
    different answers (retry versus fix permissions), so they must not collapse
    into one exception type.
    """
    database = tmp_path / "g15.db"
    holder = connect(database)
    try:
        initialize_database(holder)
        holder.execute("BEGIN IMMEDIATE")

        blocked = sqlite3.connect(str(database), isolation_level=None, timeout=0.05)
        try:
            with pytest.raises(DatabaseError) as caught:
                with transaction(blocked):
                    blocked.execute(
                        "INSERT INTO runs VALUES ('r','s','2.0','v','c','t')"
                    )
        finally:
            blocked.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert not isinstance(caught.value, DatabaseReadOnlyError)
    assert "locked" in str(caught.value).lower()


def test_the_read_only_error_is_a_database_error(tmp_path: Path) -> None:
    """The new error keeps the exit code the CLI maps it to.

    ``docs/interface.md`` section 10 maps a database failure to exit code 3.
    A new exception type must not silently fall out of that mapping.
    """
    assert issubclass(DatabaseReadOnlyError, DatabaseError)


# --------------------------------------------------------------------------
# the race guard
# --------------------------------------------------------------------------


def test_a_read_only_observer_cannot_break_the_writer(tmp_path: Path) -> None:
    """Issue #101 completion condition 3: an observer must not break the writer.

    This is the case the original guard missed: the observer has to be running
    *while* the writer creates the schema. Starting the observer only after the
    writer signalled ``ready`` (which happens after ``initialize_database``)
    tested "observe a finished schema", not the race itself, so the observer is
    started first here and the writer joins it.

    Two exit codes are not enough to prove anything: an observer that swallows
    every error also exits 0 after reading nothing. The observer therefore
    reports how many reads succeeded, and at least one is required.
    """
    database = tmp_path / "artifacts" / "results.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    stop = context.Event()
    successes = context.Queue()
    writer = context.Process(target=_child_writes, args=(str(database), ready, stop))
    observer = context.Process(
        target=_child_observes, args=(str(database), successes, stop)
    )

    # The observer goes first: the race is between creating the schema and
    # observing the file, so the two windows must overlap.
    observer.start()
    try:
        time.sleep(0.2)
        writer.start()
        assert ready.wait(60), "the writer never created the schema"
        time.sleep(1.0)
    finally:
        stop.set()
        writer.join(60)
        observer.join(60)

    assert writer.exitcode == 0, f"the writer failed: exit code {writer.exitcode}"
    assert observer.exitcode == 0, f"the observer failed: exit code {observer.exitcode}"

    reads = successes.get(timeout=10)
    assert reads >= 1, "the observer never read the database successfully"


def test_the_schema_is_created_once_and_is_idempotent(tmp_path: Path) -> None:
    """Initialising twice is a no-op, as section 9 requires."""
    database = tmp_path / "g15.db"
    connection = connect(database)
    try:
        initialize_database(connection)
        initialize_database(connection)
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()

    expected = {
        "runs",
        "processed_chunks",
        "feature_windows",
        "alarms",
        "alarm_states",
    }
    assert expected.issubset(names)


def test_every_schema_statement_is_repeatable(tmp_path: Path) -> None:
    """The schema statements carry IF NOT EXISTS, so re-running them is safe."""
    for statement in SCHEMA_STATEMENTS:
        assert "IF NOT EXISTS" in statement.upper(), statement
