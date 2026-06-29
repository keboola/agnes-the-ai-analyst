"""Transaction atomicity for multi-statement DuckDB repo mutations.

Mirrors the Postgres siblings' single-``engine.begin()`` wrapping:

  * ``ToolRegistryRepository.delete`` — the two cascade deletes (grants then
    registry row) must be one transaction so a reader never sees a grant whose
    parent tool is already gone, and a mid-cascade failure rolls both back.
  * ``ViewOwnershipRepository.reconcile`` — the read + multi-row drop is one
    transaction; a reader sees the full set or the reconciled set, never a
    partial mid-loop state.
"""

from __future__ import annotations

import duckdb
import pytest

from src.db import get_system_db
from src.repositories.tool_registry import ToolRegistryRepository
from src.repositories.view_ownership import ViewOwnershipRepository


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db

    monkeypatch.setattr(db, "_system_db_conn", None, raising=False)
    monkeypatch.setattr(db, "_system_db_path", None, raising=False)
    return get_system_db()


def _seed_tool(conn, tool_id="t1"):
    conn.execute(
        "INSERT INTO tool_registry (tool_id, source_id, original_name, exposed_name, mode) "
        "VALUES (?, ?, ?, ?, ?)",
        [tool_id, "src1", "mytool", "mytool", "read"],
    )
    conn.execute("INSERT INTO tool_grants (tool_id, group_id) VALUES (?, ?)", [tool_id, "g1"])


def test_tool_delete_removes_both_tables(conn):
    _seed_tool(conn)
    ToolRegistryRepository(conn).delete("t1")
    assert conn.execute("SELECT COUNT(*) FROM tool_registry WHERE tool_id='t1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tool_grants WHERE tool_id='t1'").fetchone()[0] == 0


def test_tool_delete_rolls_back_on_failure(conn):
    """A failure after the first cascade delete must leave BOTH rows intact —
    no orphaned half-delete."""
    _seed_tool(conn)

    class Boom(Exception):
        pass

    class Flaky:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            # Fail on the second delete (the registry row) after the grants
            # delete has already run inside the transaction.
            if sql.strip().upper().startswith("DELETE FROM TOOL_REGISTRY"):
                raise Boom()
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with pytest.raises(Boom):
        ToolRegistryRepository(Flaky(conn)).delete("t1")

    # Both rows survive — the grants delete was rolled back with the failure.
    assert conn.execute("SELECT COUNT(*) FROM tool_registry WHERE tool_id='t1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM tool_grants WHERE tool_id='t1'").fetchone()[0] == 1


def test_view_ownership_reconcile_drops_only_stale(conn):
    for s, v in [("srcA", "v1"), ("srcA", "v2"), ("srcB", "v3")]:
        conn.execute(
            "INSERT INTO view_ownership(source_name, view_name) VALUES (?, ?) "
            "ON CONFLICT DO NOTHING",
            [s, v],
        )
    dropped = ViewOwnershipRepository(conn).reconcile([("srcA", "v1")])
    assert set(dropped) == {("srcA", "v2"), ("srcB", "v3")}
    left = {tuple(r) for r in conn.execute("SELECT source_name, view_name FROM view_ownership").fetchall()}
    assert left == {("srcA", "v1")}
