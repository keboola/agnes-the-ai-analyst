"""`get_analytics_db()` is a singleton mirroring `get_system_db()` (#163).

Pre-#163 every call opened a fresh `duckdb.connect()` — most callers
don't `.close()` the returned handle, so each leaked connection held a
WAL ref + FD until GC kicked in. Under load this manifested as "too
many open files" or DuckDB lock contention on the analytics DB.

These tests pin the new contract so any regression to per-call
`duckdb.connect()` is loud:

1. Two consecutive calls return cursors backed by the same connection.
2. Closing one cursor does NOT close the underlying connection.
3. `DATA_DIR` change → fresh connection on next call.
4. Concurrent calls don't race (the lock serializes init).
5. `close_analytics_db()` clears the singleton + a subsequent call
   reopens cleanly.
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch, tmp_path):
    """Each test gets its own DATA_DIR + clean singleton state.

    Reset both globals before AND after the test so a leak from a
    previous test (this file or anywhere else in the suite) doesn't
    pollute the case under inspection.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_mod

    db_mod._analytics_db_conn = None
    db_mod._analytics_db_path = None
    yield
    db_mod._analytics_db_conn = None
    db_mod._analytics_db_path = None


def test_get_analytics_db_caches_connection():
    """Two consecutive calls must share the same underlying DuckDB
    connection object — not open a fresh one each time."""
    from src.db import get_analytics_db
    import src.db as db_mod

    cur1 = get_analytics_db()
    cur2 = get_analytics_db()
    # Cursors are different objects (DuckDB returns a fresh cursor each
    # call) but they're both backed by `_analytics_db_conn` — only one
    # underlying connection should have been opened.
    assert db_mod._analytics_db_conn is not None
    assert cur1 is not cur2  # cursors differ
    # Sanity: both cursors execute against the same DB by writing +
    # reading via the shared connection.
    cur1.execute("CREATE TABLE singleton_probe (x INTEGER)")
    cur2.execute("INSERT INTO singleton_probe VALUES (42)")
    rows = cur1.execute("SELECT x FROM singleton_probe").fetchall()
    assert rows == [(42,)]


def test_closing_cursor_does_not_close_connection():
    """The whole point of `.cursor()` indirection — close the cursor
    handle, the underlying connection stays usable for the next call."""
    from src.db import get_analytics_db
    import src.db as db_mod

    cur1 = get_analytics_db()
    cur1.execute("CREATE TABLE probe (x INTEGER)")
    cur1.close()  # caller is allowed to do this; mustn't break #2 call
    # The connection itself must still be alive on the singleton.
    assert db_mod._analytics_db_conn is not None
    cur2 = get_analytics_db()
    rows = cur2.execute("SELECT COUNT(*) FROM probe").fetchall()
    assert rows == [(0,)]


def test_get_analytics_db_reopens_on_data_dir_change(tmp_path, monkeypatch):
    """When DATA_DIR (the resolved path) changes, the singleton must
    drop the old connection and open a fresh one against the new path.
    This is the test-fixture path — production never moves DATA_DIR
    mid-process, but pytest fixtures do."""
    import src.db as db_mod
    from src.db import get_analytics_db

    cur1 = get_analytics_db()
    cur1.execute("CREATE TABLE marker_a (x INTEGER)")
    conn_a = db_mod._analytics_db_conn

    # Move to a new DATA_DIR — singleton must reopen.
    new_dir = tmp_path.parent / "alt-data"
    new_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(new_dir))
    cur2 = get_analytics_db()
    conn_b = db_mod._analytics_db_conn

    assert conn_a is not conn_b, "singleton should have reopened on DATA_DIR change"
    # The new DB doesn't have marker_a — confirms it's a fresh DB at the new path.
    with pytest.raises(Exception):
        cur2.execute("SELECT * FROM marker_a")


def test_get_analytics_db_thread_safe():
    """Concurrent calls from N threads must produce exactly ONE
    underlying connection (the lock serializes the init branch)."""
    from src.db import get_analytics_db
    import src.db as db_mod

    errors: list[BaseException] = []
    cursors: list = []

    def worker():
        try:
            cur = get_analytics_db()
            cursors.append(cur)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], errors
    assert len(cursors) == 16
    # All cursors share one connection.
    assert db_mod._analytics_db_conn is not None
    # Any race-induced second connection would be re-assigned and the
    # first would orphan; we can't probe that directly, but functionally
    # all 16 threads must see the SAME singleton state.
    cursors[0].execute("CREATE TABLE thread_probe (x INTEGER)")
    rows = cursors[15].execute("SELECT COUNT(*) FROM thread_probe").fetchall()
    assert rows == [(0,)], "16th thread's cursor doesn't see the 1st's table — race"


def test_close_analytics_db_clears_singleton_and_reopen_works():
    """Shutdown hook clears the singleton; a subsequent call after
    re-init (test process keeps running) must reopen cleanly."""
    import src.db as db_mod
    from src.db import close_analytics_db, get_analytics_db

    cur1 = get_analytics_db()
    cur1.execute("CREATE TABLE probe (x INTEGER)")
    assert db_mod._analytics_db_conn is not None

    close_analytics_db()
    assert db_mod._analytics_db_conn is None
    assert db_mod._analytics_db_path is None

    # Re-open after close: fresh cursor, table from previous session
    # PERSISTS on disk (we close, not nuke).
    cur2 = get_analytics_db()
    rows = cur2.execute("SELECT COUNT(*) FROM probe").fetchall()
    assert rows == [(0,)]


class TestReadonlyOnFreshDataDir:
    """`get_analytics_db_readonly()` must stay usable on a fresh install.

    Regression: when ``analytics/server.duckdb`` did not exist yet, the
    read-only factory created it with a **read-write** connection and
    handed that back. The file then existed, so every later call took the
    read-only branch — and DuckDB refuses to open a file read-only while a
    read-write connection to it is alive in the same process:

        ConnectionException: Can't open a connection to same database file
        with a different configuration than existing connections

    `app/api/query.py` never closes the handle, so on a fresh instance the
    first request touching the query path poisoned every subsequent query
    in that process until restart.
    """

    def test_second_call_on_fresh_data_dir_does_not_raise(self):
        from src.db import get_analytics_db_readonly

        first = get_analytics_db_readonly()
        assert first.execute("SELECT 1").fetchone() == (1,)

        # The poisoning call: file now exists → read-only branch.
        second = get_analytics_db_readonly()
        assert second.execute("SELECT 1").fetchone() == (1,)

    def test_readonly_handle_cannot_write(self):
        """The connection handed to the query path must be read-only even
        on the very first call, when the factory had to create the file."""
        import duckdb
        from src.db import get_analytics_db_readonly

        conn = get_analytics_db_readonly()
        with pytest.raises(duckdb.Error):
            conn.execute("CREATE TABLE writes_should_fail (x INTEGER)")


class TestReadonlyFreshDataDirConcurrency:
    """Unsynchronized create-then-reopen on a fresh data dir.

    `get_analytics_db_readonly()` materializes `analytics/server.duckdb`
    with a transient read-write handle when it doesn't exist yet, then
    opens a read-only handle. Without a lock around that sequence, thread A
    can be mid-materialization (read-write handle still alive) while
    thread B — having observed the file already exists — reaches its own
    read-only open first, and DuckDB raises "Can't open a connection to
    same database file with a different configuration than existing
    connections", 500-ing thread B's request.

    This exercises real concurrent threads (not just an assertion that a
    lock object is referenced): it widens the race window by delaying the
    return of the read-write open, then fires several threads at a fresh
    `DATA_DIR` and asserts none of them raise.
    """

    def test_concurrent_first_calls_do_not_raise(self, monkeypatch, tmp_path):
        import time

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import src.db as db_mod

        real_open = db_mod._open_duckdb

        def slow_open(path, **kwargs):
            conn = real_open(path, **kwargs)
            if kwargs.get("read_only") is False:
                # Keep the transient read-write handle (materialization
                # branch) alive a little longer so a concurrent thread's
                # read-only open — if not properly serialized — reliably
                # lands while it's still open, instead of only sometimes.
                time.sleep(0.1)
            return conn

        monkeypatch.setattr(db_mod, "_open_duckdb", slow_open)

        errors: list[BaseException] = []

        def worker():
            try:
                conn = db_mod.get_analytics_db_readonly()
                assert conn.execute("SELECT 1").fetchone() == (1,)
                conn.close()
            except BaseException as e:  # noqa: BLE001 - collecting for assertion below
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], errors
