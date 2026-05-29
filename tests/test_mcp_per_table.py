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

import os
import uuid

import duckdb
import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from src.db import get_analytics_db, get_system_db
from src.repositories.table_registry import TableRegistryRepository


def _seed_view_and_registry(rows: list[dict]) -> dict:
    """Insert a fresh view into analytics.duckdb + register the table.

    Returns ``{table_id}`` — table_id doubles as the view name (per the
    orchestrator's contract). Each call uses a fresh id so multiple
    tests can share the same analytics DB without colliding.
    """
    table_id = f"tt_{uuid.uuid4().hex[:8]}"

    # Analytics DB: create the view the endpoint will SELECT from. We
    # write through the same pooled connection the app uses so the
    # path resolution stays in one place. The endpoint reopens it
    # read-only — DuckDB allows concurrent readers next to a writer
    # in this configuration.
    a_conn = get_analytics_db()
    cols = sorted(rows[0].keys()) if rows else ["id"]
    select_parts = []
    for r in rows:
        vals = ", ".join(
            (f"'{r[c]}'" if isinstance(r[c], str) else str(r[c])) + f" AS \"{c}\""
            for c in cols
        )
        select_parts.append(f"SELECT {vals}")
    union_sql = " UNION ALL ".join(select_parts) if select_parts else "SELECT NULL AS id"
    a_conn.execute(f'CREATE OR REPLACE VIEW "{table_id}" AS {union_sql}')

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
    seed = _seed_view_and_registry([
        {"id": "1", "country": "CZ"},
        {"id": "2", "country": "DE"},
        {"id": "3", "country": "CZ"},
    ])
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
    seed = _seed_view_and_registry([
        {"id": "1", "country": "CZ"},
        {"id": "2", "country": "DE"},
        {"id": "3", "country": "CZ"},
    ])
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
        id=table_id, name=table_id, folder=None,
        sync_strategy="full_refresh", registered_by="system_seed",
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
