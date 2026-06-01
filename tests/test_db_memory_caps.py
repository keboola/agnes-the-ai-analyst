"""Per-connection DuckDB memory caps (system.duckdb resilience).

DuckDB enforces ``memory_limit`` per-connection, not per process. The
``system.duckdb`` singleton was previously uncapped — in a memory-bounded
container it could grow the process past the cgroup cap on its own and the
kernel OOM-killed everything. ``get_system_db`` must now apply an explicit
budget (mirroring the analytics path) so the connections sum under the cap.
"""
from __future__ import annotations

import duckdb


def _fresh_system_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module

    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module, db_module.get_system_db()


def test_system_connection_has_memory_cap(tmp_path, monkeypatch):
    """The system connection reports the configured cap, not DuckDB's
    cgroup/host default. Compared against a control connection so the
    assertion is independent of how DuckDB formats the limit string."""
    db_module, conn = _fresh_system_db(tmp_path, monkeypatch)

    sys_limit = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]

    control = duckdb.connect()
    control.execute(f"SET memory_limit='{db_module._SYSTEM_DB_MEMORY_LIMIT}'")
    expected = control.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    control.close()

    assert sys_limit == expected
    threads = conn.execute("SELECT current_setting('threads')").fetchone()[0]
    assert int(threads) == db_module._DUCKDB_THREADS


def test_apply_memory_caps_swallows_pragma_failure(tmp_path, monkeypatch):
    """A connection whose SET PRAGMAs raise must not blow up the helper —
    the connection stays usable on its defaults."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module

    class _BadConn:
        def execute(self, *args, **kwargs):
            raise duckdb.Error("boom")

    # Must return normally (no exception escapes).
    db_module._apply_memory_caps(_BadConn(), "1GB", label="test")


def test_apply_memory_caps_sets_temp_directory(tmp_path, monkeypatch):
    """The spill directory is configured so an over-budget query spills to
    disk instead of growing process RSS."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module

    conn = duckdb.connect()
    db_module._apply_memory_caps(conn, "1GB", label="test")
    temp_dir = conn.execute("SELECT current_setting('temp_directory')").fetchone()[0]
    conn.close()
    assert temp_dir, "temp_directory should be set for disk spill"
    assert "duckdb-tmp" in temp_dir
