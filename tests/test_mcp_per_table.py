"""Tests for the per-table outbound MCP tool surface (RFC #461 §7).

Pre-populates analytics.duckdb with a tiny view (no orchestrator
needed) and exercises the four shapes of the new
``POST /api/mcp/query-table/{table_id}`` endpoint:

* admin can query any registered table; rows come back as JSON;
* filter on a known column reduces the result;
* filter on an unknown column returns 400 + the allowed columns;
* missing analytics view (registered but not synced) returns 409;
* unknown table returns 404.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from src.db import close_analytics_db, get_analytics_db, get_system_db
from src.repositories.table_registry import TableRegistryRepository
from src.sql_ident import quote_ident


def _seed_view_and_registry(rows: list[dict]) -> dict:
    """Insert a fresh view into analytics.duckdb + register the table.

    Returns ``{table_id}`` — table_id doubles as the view name (per the
    orchestrator's contract). Each call uses a fresh id so multiple
    tests can share the same analytics DB without colliding.
    """
    table_id = f"tt_{uuid.uuid4().hex[:8]}"

    # Analytics DB: create the view the endpoint will SELECT from, then
    # close the read-write singleton. The endpoint now opens a fresh
    # *read-only* connection per call (`get_analytics_db_readonly()`) —
    # DuckDB refuses to open a file read-only while a read-write
    # connection to it is alive in the same process, so a lingering
    # writer left open by seeding would poison every query below.
    a_conn = get_analytics_db()
    cols = sorted(rows[0].keys()) if rows else ["id"]
    select_parts = []
    for r in rows:
        vals = ", ".join((f"'{r[c]}'" if isinstance(r[c], str) else str(r[c])) + f' AS "{c}"' for c in cols)
        select_parts.append(f"SELECT {vals}")
    union_sql = " UNION ALL ".join(select_parts) if select_parts else "SELECT NULL AS id"
    a_conn.execute(f'CREATE OR REPLACE VIEW "{table_id}" AS {union_sql}')
    close_analytics_db()

    # System DB: register the table so the endpoint can find it
    sys_conn = get_system_db()
    TableRegistryRepository(sys_conn).register(
        id=table_id,
        name=table_id,
        folder=None,
        sync_strategy="full_refresh",
        registered_by="system_seed",
    )
    sys_conn.close()
    return {"table_id": table_id}


# ── happy paths ──────────────────────────────────────────────────────────


def test_query_table_returns_rows(seeded_app):
    seed = _seed_view_and_registry(
        [
            {"id": "1", "country": "CZ"},
            {"id": "2", "country": "DE"},
            {"id": "3", "country": "CZ"},
        ]
    )
    r = seeded_app["client"].post(
        f"/api/mcp/query-table/{seed['table_id']}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table_id"] == seed["table_id"]
    assert body["row_count"] == 3
    assert set(body["columns"]) == {"id", "country"}
    assert body["truncated"] is False


def test_query_table_filter_reduces_result(seeded_app):
    seed = _seed_view_and_registry(
        [
            {"id": "1", "country": "CZ"},
            {"id": "2", "country": "DE"},
            {"id": "3", "country": "CZ"},
        ]
    )
    r = seeded_app["client"].post(
        f"/api/mcp/query-table/{seed['table_id']}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"filter": {"country": "CZ"}, "limit": 10},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["row_count"] == 2
    assert all(row["country"] == "CZ" for row in body["rows"])


def test_query_table_limit_caps_to_max(seeded_app):
    seed = _seed_view_and_registry([{"id": str(i)} for i in range(5)])
    r = seeded_app["client"].post(
        f"/api/mcp/query-table/{seed['table_id']}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"filter": {}, "limit": 100_000},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["truncated"] is True
    assert body["row_count"] == 5  # only 5 rows in the source, even though cap is 1000


# ── 400 / 403 / 404 / 409 ────────────────────────────────────────────────


def test_query_table_400_for_unknown_filter_column(seeded_app):
    seed = _seed_view_and_registry([{"id": "1", "country": "CZ"}])
    r = seeded_app["client"].post(
        f"/api/mcp/query-table/{seed['table_id']}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"filter": {"continent": "EU"}, "limit": 10},
    )
    assert r.status_code == 400
    body = r.json()
    detail = body["detail"]
    assert detail["error"] == "unknown_filter_columns"
    assert detail["unknown"] == ["continent"]
    assert "id" in detail["allowed"]
    assert "country" in detail["allowed"]


def test_query_table_404_for_unknown_table(seeded_app):
    r = seeded_app["client"].post(
        "/api/mcp/query-table/tt_does_not_exist",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 404


def test_query_table_409_for_registered_but_unsynced_table(seeded_app):
    """Registry has the row but analytics has no view → 409."""
    table_id = f"tt_unsync_{uuid.uuid4().hex[:6]}"
    sys_conn = get_system_db()
    TableRegistryRepository(sys_conn).register(
        id=table_id,
        name=table_id,
        folder=None,
        sync_strategy="full_refresh",
        registered_by="system_seed",
    )
    sys_conn.close()

    r = seeded_app["client"].post(
        f"/api/mcp/query-table/{table_id}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 409


def test_query_table_400_for_limit_zero(seeded_app):
    seed = _seed_view_and_registry([{"id": "1"}])
    r = seeded_app["client"].post(
        f"/api/mcp/query-table/{seed['table_id']}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"filter": {}, "limit": 0},
    )
    assert r.status_code == 400


def test_query_table_filter_column_name_cannot_break_out_of_its_quotes(seeded_app):
    """A column whose NAME carries a quote is an identifier, not SQL.

    The filter allow-list upstream only checks that the column exists in the
    view's ``DESCRIBE`` output — it never constrained the characters in the
    name. Column names for a collection-ingested table are whatever the
    uploaded file's header said: ``src/ingest/tabular.py`` COPYs the reader's
    output straight to parquet with no renaming. So a header of
    ``x") OR 1=1 --`` becomes a real column, passes the allow-list, and — while
    the WHERE clause was built as ``f'"{col}" = ?'`` — closed the quoted
    identifier and appended its own predicate.

    Asserting "no 500" is not enough: a successful break-out yields a valid
    query too. The test pins the semantics instead — the filter must behave
    like a filter on that column, so a non-matching value returns nothing.
    ``OR 1=1`` would return every row.
    """
    evil = 'x") OR 1=1 --'
    table_id = f"tt_{uuid.uuid4().hex[:8]}"

    a_conn = get_analytics_db()
    a_conn.execute(
        f"CREATE OR REPLACE VIEW {quote_ident(table_id)} AS "
        f"SELECT 'keep' AS {quote_ident(evil)} UNION ALL SELECT 'other' AS {quote_ident(evil)}"
    )
    close_analytics_db()  # release the RW singleton — see _seed_view_and_registry
    sys_conn = get_system_db()
    TableRegistryRepository(sys_conn).register(
        id=table_id,
        name=table_id,
        folder=None,
        sync_strategy="full_refresh",
        registered_by="system_seed",
    )
    sys_conn.close()

    def _query(value: str):
        return seeded_app["client"].post(
            f"/api/mcp/query-table/{table_id}",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
            json={"filter": {evil: value}, "limit": 10},
        )

    # The column really is queryable under its hostile name.
    hit = _query("keep")
    assert hit.status_code == 200, hit.text
    assert hit.json()["row_count"] == 1

    # And it filters. Pre-fix this returned both rows via the injected OR 1=1.
    miss = _query("no-such-value")
    assert miss.status_code == 200, miss.text
    assert miss.json()["row_count"] == 0


# ── analytics-DB connection lifecycle ───────────────────────────────────


def test_query_table_does_not_leave_readonly_path_poisoned(seeded_app):
    """Regression: a call to this endpoint must not permanently break
    ``/api/query`` and ``/api/query/hybrid`` for the rest of the process.

    Pre-fix this endpoint opened (and never closed) ``get_analytics_db()``'s
    process-wide read-write singleton. Any authenticated user hitting
    ``POST /api/mcp/query-table/{id}`` once therefore left that singleton
    open for the process's remaining lifetime, and DuckDB refuses to open a
    file read-only while a read-write connection to it is alive in the same
    process — so every later ``get_analytics_db_readonly()`` call (the path
    ``/api/query`` and ``/api/query/hybrid`` use) raised
    ``ConnectionException: Can't open a connection to same database file
    with a different configuration than existing connections``,
    deterministically, until restart.
    """
    import src.db as db_mod
    from src.db import get_analytics_db_readonly

    seed = _seed_view_and_registry([{"id": "1"}])
    r = seeded_app["client"].post(
        f"/api/mcp/query-table/{seed['table_id']}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"filter": {}, "limit": 10},
    )
    assert r.status_code == 200, r.text

    # The endpoint must not have opened the read-write singleton.
    assert db_mod._analytics_db_conn is None

    # The exact sequence that used to poison every later request.
    ro = get_analytics_db_readonly()
    try:
        assert ro.execute("SELECT 1").fetchone() == (1,)
    finally:
        ro.close()
