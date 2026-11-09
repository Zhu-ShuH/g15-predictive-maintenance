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

import ctypes
import ctypes.wintypes as wintypes
import multiprocessing
import os
import sqlite3
import time
from pathlib import Path

import pytest

from g15.storage import database as database_module
from g15.storage.database import (
    SCHEMA_STATEMENTS,
    DatabaseError,
    DatabaseReadOnlyError,
    connect,
    connect_readonly,
    describe_read_only_context,
    initialize_database,
    transaction,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Win32 constants for the sidecar hold that reproduces the Issue #123
#: trigger.  ``FILE_SHARE_READ`` alone denies write access to every other
#: handle, which is what makes the writer's own open of the file fail and
#: get silently downgraded to read-only by SQLite's VFS.
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80


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


def _child_holds_sidecars(database: str, ready, stop, hold_seconds: float = 0.3) -> None:  # type: ignore[no-untyped-def]
    """Hold the WAL sidecar files open without write sharing.

    This is the deterministic form of the Issue #123 trigger.  On Windows,
    SQLite's VFS silently downgrades a read/write open of the ``-wal`` and
    ``-shm`` files to a read-only one when another process holds them
    without write sharing, and it caches that decision on the WAL handle.
    The writer then fails its first write with a bare ``SQLITE_READONLY``
    (primary code 8, extended code 0).

    The files are opened with ``CreateFileW`` and a share mode of
    ``FILE_SHARE_READ``, which denies write access to any other handle --
    that is what makes the writer's own open fail and get downgraded.  On
    other platforms the trigger does not exist and this helper waits.

    Args:
        database: The database whose sidecar files to hold.
        ready: Set once the files are held.
        stop: Set to end the hold early.
        hold_seconds: How long to hold the files when ``stop`` is not set.
    """
    if os.name != "nt":
        ready.set()
        stop.wait(30)
        return
    path = Path(database)
    handles = []
    try:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            handle = _open_without_write_sharing(sidecar)
            if handle is not None:
                handles.append(handle)
        ready.set()
        # Hold the files only briefly by default.  A real reader holds them
        # for the duration of one query, so the writer's re-open has to be
        # able to succeed once the holder lets go.
        stop.wait(hold_seconds)
    finally:
        for handle in handles:
            _close_handle(handle)


def _open_without_write_sharing(path: Path) -> int | None:
    """Open ``path`` so that no other handle may write to it.

    Args:
        path: The file to open.

    Returns:
        The Win32 handle, or ``None`` when the file could not be opened.
    """
    if os.name != "nt":  # pragma: no cover - the guard is Windows-only
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    handle = kernel32.CreateFileW(
        ctypes.c_wchar_p(str(path)),
        _GENERIC_READ,
        _FILE_SHARE_READ,
        None,
        _OPEN_ALWAYS,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        return None
    return int(handle)


def _close_handle(handle: int) -> None:
    """Close a Win32 handle opened by :func:`_open_without_write_sharing`."""
    if os.name != "nt":  # pragma: no cover - the guard is Windows-only
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle(wintypes.HANDLE(handle))


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


# --------------------------------------------------------------------------
# the Issue #123 guard: a degraded WAL handle must not reach the consumer
# --------------------------------------------------------------------------


def test_the_writer_survives_a_degraded_wal_handle(tmp_path: Path) -> None:
    """Issue #123: the writer must not inherit a read-only WAL handle.

    On Windows, SQLite's VFS silently downgrades a read/write open of the
    ``-wal`` and ``-shm`` files to a read-only one when another process holds
    them without write sharing, and it caches that decision on the WAL
    handle.  The writer then fails its first write with a bare
    ``SQLITE_READONLY`` -- primary code 8, extended code 0 -- which is
    exactly what the independent review measured.

    The storage layer therefore checks that the handle it got back can
    write, and re-opens the database when it cannot.  This test reproduces
    the boundary deterministically: another process holds the sidecar files
    without write sharing while the writer opens the database, then lets go
    shortly afterwards -- the shape of a short-lived reader.

    Without the check, the writer keeps the degraded handle and fails.
    """
    if os.name != "nt":
        pytest.skip("the WAL handle degradation is a Windows VFS behaviour")

    database = tmp_path / "artifacts" / "results.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)

    # The schema has to exist before the writer can write a row into it.
    seed = connect(database)
    try:
        initialize_database(seed)
    finally:
        seed.close()

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    stop = context.Event()
    holder = context.Process(
        target=_child_holds_sidecars, args=(str(database), ready, stop, 0.3)
    )
    holder.start()
    try:
        assert ready.wait(30), "the holder never took the sidecar files"
        # The writer opens the database while the sidecar files are held
        # without write sharing.  It must still end up with a writable
        # handle: it re-opens until the holder lets go.
        connection = connect(database)
        try:
            with transaction(connection):
                connection.execute(
                    "INSERT INTO runs VALUES ('r','s','2.0','v','c','2026-01-01')"
                )
        finally:
            connection.close()
    finally:
        stop.set()
        holder.join(30)

    assert holder.exitcode == 0, f"the holder failed: exit code {holder.exitcode}"


def test_a_degraded_handle_is_reported_with_its_diagnostics(tmp_path: Path) -> None:
    """A permanently held sidecar file is named, with the context.

    When the sidecar files are held for good, no re-open can succeed and the
    writer has to fail.  The failure must then say what refused the write --
    the codes, the path, the journal mode and the sidecar state -- so that
    the cause is determinable from the log rather than guessed at.
    """
    if os.name != "nt":
        pytest.skip("the WAL handle degradation is a Windows VFS behaviour")

    database = tmp_path / "artifacts" / "results.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    stop = context.Event()
    holder = context.Process(
        target=_child_holds_sidecars, args=(str(database), ready, stop, 30.0)
    )
    holder.start()
    try:
        assert ready.wait(30), "the holder never took the sidecar files"
        with pytest.raises(DatabaseReadOnlyError) as caught:
            connect(database)
    finally:
        stop.set()
        holder.join(30)

    message = str(caught.value)
    assert "sqlite primary=8" in message
    assert "database=" in message
    assert "-shm=" in message


def test_a_refused_write_names_the_diagnostic_context(tmp_path: Path) -> None:
    """A read-only refusal carries the context Issue #123 asked for.

    The bare ``SQLITE_READONLY`` says only that a write was refused.  The
    review asked for the primary and extended codes, the absolute path, the
    journal mode and the state of the WAL sidecar files, so that a failure
    can be diagnosed from the log alone.
    """
    database = tmp_path / "g15.db"
    connection = connect(database)
    try:
        initialize_database(connection)
        context = describe_read_only_context(database, connection=connection)
    finally:
        connection.close()

    assert "database=" in context
    assert "journal_mode=wal" in context
    assert "-shm=" in context
    assert "-wal=" in context


def test_the_diagnostic_context_reports_the_sqlite_codes(tmp_path: Path) -> None:
    """The diagnostic context names the SQLite primary and extended codes.

    ``sqlite_errorcode`` carries the *full* result code, so the primary code
    sits in the low byte and the extended code in the upper bits.  Reporting
    the full code as the extended one would lose exactly the number that
    tells the flavours of a read-only refusal apart.
    """
    database = tmp_path / "g15.db"
    connection = connect(database)
    try:
        initialize_database(connection)
        error = sqlite3.OperationalError("attempt to write a readonly database")
        error.sqlite_errorcode = 8  # type: ignore[attr-defined]
        context = describe_read_only_context(database, error, connection=connection)
    finally:
        connection.close()

    assert "primary=8" in context
    assert "extended=0" in context


def test_the_diagnostic_context_tells_the_readonly_flavours_apart(
    tmp_path: Path,
) -> None:
    """The extended code separates the read-only refusals from each other.

    A bare ``SQLITE_READONLY`` (full code 8) and
    ``SQLITE_READONLY_CANTINIT`` (full code 776) share the primary code 8 and
    differ only in the extended code -- 0 against 3.  The diagnostic context
    has to report that difference, because it is what tells "the handle was
    downgraded" apart from "the database could not be initialised at all".
    """
    database = tmp_path / "g15.db"
    connection = connect(database)
    try:
        initialize_database(connection)
        error = sqlite3.OperationalError("attempt to write a readonly database")
        error.sqlite_errorcode = 776  # type: ignore[attr-defined]
        error.sqlite_errorname = "SQLITE_READONLY_CANTINIT"  # type: ignore[attr-defined]
        context = describe_read_only_context(database, error, connection=connection)
    finally:
        connection.close()

    assert "primary=8" in context
    assert "extended=3" in context
    assert "SQLITE_READONLY_CANTINIT" in context


def test_a_probe_that_cannot_roll_back_does_not_return_a_broken_connection(
    tmp_path: Path,
) -> None:
    """A failed probe rollback must not hand out a connection in a transaction.

    The write-capability probe opens a transaction and rolls it back.  If the
    rollback fails, the connection is left inside a transaction that nobody
    asked for, and the caller's next ``BEGIN`` fails with "cannot start a
    transaction within a transaction".  Returning such a connection would move
    the failure from the probe to a place that cannot explain it, so the
    refusal has to happen here instead.
    """
    database = tmp_path / "g15.db"
    connection = connect(database)
    try:
        initialize_database(connection)
    finally:
        connection.close()

    original = database_module._open_and_configure

    def refusing_open(path: Path) -> sqlite3.Connection:
        opened = original(path)
        opened.set_authorizer(
            lambda action, arg1, _arg2, _database, _trigger: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and arg1 == "ROLLBACK"
                else sqlite3.SQLITE_OK
            )
        )
        return opened

    monkeypatch_target = database_module
    monkeypatch_target._open_and_configure = refusing_open
    try:
        with pytest.raises(DatabaseError) as caught:
            connect(database)
    finally:
        monkeypatch_target._open_and_configure = original

    assert "could not be rolled back" in str(caught.value)


def test_the_probe_does_not_wait_for_a_concurrent_writer(tmp_path: Path) -> None:
    """The probe must not block for the busy timeout under a concurrent writer.

    Probing with ``BEGIN IMMEDIATE`` used to wait for the connection's busy
    timeout -- five seconds by default -- before SQLite reported
    ``SQLITE_BUSY``.  The wait bought nothing: ``SQLITE_BUSY`` is lock
    contention rather than a degraded handle, so the probe answered "writable"
    either way, just five seconds later and on every concurrent consumer.
    """
    database = tmp_path / "g15.db"
    holder = connect(database)
    try:
        initialize_database(holder)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO runs VALUES ('r','s','2.0','v','c','2026-01-01')")

        started = time.monotonic()
        connection = connect(database)
        elapsed = time.monotonic() - started
        try:
            assert not connection.in_transaction
        finally:
            connection.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert elapsed < 1.0, f"the probe waited {elapsed:.3f}s for the busy timeout"
