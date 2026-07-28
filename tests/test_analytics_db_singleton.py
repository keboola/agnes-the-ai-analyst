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
