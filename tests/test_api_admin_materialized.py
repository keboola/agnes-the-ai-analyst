"""Admin API accepts source_query when query_mode='materialized', rejects
mismatches between mode and query field."""
import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_materialized_requires_source_query(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/register-table", json={
        "name": "orders_90d",
        "source_type": "bigquery",
        "query_mode": "materialized",
        # source_query missing
    }, headers=_auth(token))
    assert 400 <= r.status_code < 500, r.json()
    detail = str(r.json().get("detail", "")).lower()
    assert "source_query" in detail or "materialized" in detail


def test_register_materialized_accepts_source_query(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/register-table", json={
        "name": "orders_90d_b7_a",
        "source_type": "bigquery",
        "query_mode": "materialized",
        "source_query": "SELECT date FROM bq.\"prj.ds.orders\"",
        "sync_schedule": "every 6h",
    }, headers=_auth(token))
    assert r.status_code == 201, r.json()
    assert r.json()["status"] == "registered"


def test_register_remote_rejects_source_query(seeded_app):
    """source_query is only meaningful with materialized mode."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/register-table", json={
        "name": "live_orders_b7",
        "source_type": "bigquery",
        "query_mode": "remote",
        "source_query": "SELECT 1",
    }, headers=_auth(token))
    assert 400 <= r.status_code < 500, r.json()


def test_register_local_rejects_source_query(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/register-table", json={
        "name": "kbc_orders_b7",
        "source_type": "keboola",
        "query_mode": "local",
        "source_query": "SELECT 1",
    }, headers=_auth(token))
    assert 400 <= r.status_code < 500, r.json()


def test_update_materialized_to_remote_rejects_keeping_query(seeded_app):
    """Updating an existing materialized table to remote must drop the query."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # First register a materialized table.
    r = c.post("/api/admin/register-table", json={
        "name": "u_test_b7",
        "source_type": "bigquery",
        "query_mode": "materialized",
        "source_query": "SELECT 1",
    }, headers=_auth(token))
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    # Then try to PUT mode=remote while keeping the source_query — should fail.
    r2 = c.put(f"/api/admin/registry/{table_id}", json={
        "query_mode": "remote",
        "source_query": "SELECT 1",
    }, headers=_auth(token))
    assert 400 <= r2.status_code < 500, r2.json()


def test_register_materialized_with_empty_source_query_is_rejected(seeded_app):
    """Empty string is treated the same as missing — materialized needs a real query."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/register-table", json={
        "name": "empty_b7",
        "source_type": "bigquery",
        "query_mode": "materialized",
        "source_query": "",
    }, headers=_auth(token))
    assert 400 <= r.status_code < 500, r.json()


def test_update_source_query_alone_requires_query_mode(seeded_app):
    """PUT with source_query but no query_mode in the body must be rejected
    so non-materialized rows can't carry an orphan source_query."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # First seed a remote-mode row.
    r = c.post("/api/admin/register-table", json={
        "name": "live_orphan_b7",
        "source_type": "bigquery",
        "query_mode": "remote",
    }, headers=_auth(token))
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    # Try to add a source_query without changing query_mode — should fail.
    r2 = c.put(f"/api/admin/registry/{table_id}", json={
        "source_query": "SELECT 1",
    }, headers=_auth(token))
    assert 400 <= r2.status_code < 500, r2.json()
