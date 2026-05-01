"""POST /api/query for a table id that's registered as
`query_mode='materialized'` but isn't yet a view in `analytics.duckdb`
returns a helpful, materialize-aware error instead of a raw "Table does
not exist" string from DuckDB.

E2E sub-agent finding 2026-05-01: `da query --remote "SELECT * FROM
e2e2_synced_table LIMIT 5"` on a synced materialized table failed with
DuckDB's bare error message even though the table is in the registry.
The fix improves the surfaced message so the operator sees the
materialize-mode hint without having to decode DuckDB internals.
"""
from __future__ import annotations

import pytest

from src.repositories.table_registry import TableRegistryRepository


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_query_materialized_id_not_in_views_returns_helpful_message(seeded_app):
    """An admin querying a materialized id that isn't yet materialized in
    the local analytics.duckdb gets a 400 whose detail names the
    query_mode and points at `da sync` / direct-BQ-query."""
    from src.db import get_system_db
    sys_conn = get_system_db()
    try:
        TableRegistryRepository(sys_conn).register(
            id="not_yet_materialized",
            name="not_yet_materialized",
            source_type="bigquery",
            query_mode="materialized",
            source_query='SELECT 1 FROM bq."ds"."t"',
            bucket="ds",
            source_table="t",
        )
    finally:
        sys_conn.close()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM not_yet_materialized LIMIT 5"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.json()
    detail = str(r.json().get("detail", ""))
    # Message should name the table and surface the materialize-mode hint.
    assert "not_yet_materialized" in detail
    assert "materialized" in detail.lower()
    # Either a `da sync` hint or a direct-BQ-query hint must appear so the
    # operator has a concrete next step.
    assert "da sync" in detail or "bq." in detail


def test_query_unknown_table_falls_back_to_default_error(seeded_app):
    """Sanity: a query for a table that isn't even in the registry still
    surfaces DuckDB's error verbatim (no false positive on the new hint
    path). RBAC's 403 path takes precedence for non-admin callers; for
    admins (no RBAC filter) the table simply doesn't exist as a view, and
    the query falls through to DuckDB's "does not exist" message."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM totally_unknown_table"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.json()
    detail = str(r.json().get("detail", "")).lower()
    # Falls back to the generic query-error path; no materialized hint.
    assert "materialized" not in detail
