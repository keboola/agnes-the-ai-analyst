"""Admin API accepts source_query when query_mode='materialized', rejects
mismatches between mode and query field.

Covers PR #145 (re-implementation against 0.24.0 base):
- RegisterTableRequest + UpdateTableRequest model_validators
- _validate_bigquery_register_payload materialized branch (skips bucket/
  source_table checks, requires source_query)
- register_table 201 response for materialized BQ rows (no synchronous
  materialize — cron tick or manual /api/sync/trigger picks them up)
- update_table clears stale source_query when switching mode away from
  materialized

Shares the seeded_app + bq_instance fixtures from conftest /
test_admin_bq_register.py for parity with the existing BQ test surface.
"""
from unittest.mock import MagicMock

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bq_instance(monkeypatch):
    """Force instance.yaml to look like a BigQuery deployment.

    Mirrors tests/test_admin_bq_register.py::bq_instance so the
    project_id read inside _validate_bigquery_register_payload succeeds.
    """
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {"project": "my-test-project", "location": "us"},
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    yield fake_cfg
    reset_cache()


def _materialized_payload(**overrides):
    p = {
        "name": "orders_90d",
        "source_type": "bigquery",
        "query_mode": "materialized",
        "source_query": "SELECT date FROM `prj.ds.orders`",
        "sync_schedule": "every 6h",
    }
    p.update(overrides)
    return p


def test_register_materialized_requires_source_query(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "missing_query",
            "source_type": "bigquery",
            "query_mode": "materialized",
            # source_query missing
        },
        headers=_auth(token),
    )
    assert 400 <= r.status_code < 500, r.json()
    detail = str(r.json().get("detail", "")).lower()
    assert "source_query" in detail or "materialized" in detail


def test_register_materialized_accepts_source_query(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_materialized_payload(name="orders_90d_a"),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["status"] == "registered"
    assert "Materialized" in body.get("message", "")


def test_register_remote_rejects_source_query(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "live_orders",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "query_mode": "remote",
            "source_query": "SELECT 1",
        },
        headers=_auth(token),
    )
    assert 400 <= r.status_code < 500, r.json()


def test_register_local_rejects_source_query(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "kbc_orders",
            "source_type": "keboola",
            "query_mode": "local",
            "source_query": "SELECT 1",
        },
        headers=_auth(token),
    )
    assert 400 <= r.status_code < 500, r.json()


def test_register_materialized_with_empty_source_query_rejected(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_materialized_payload(name="empty_q", source_query=""),
        headers=_auth(token),
    )
    assert 400 <= r.status_code < 500, r.json()


def test_update_source_query_alone_requires_query_mode(seeded_app, bq_instance):
    """PUT body with source_query but no query_mode is incoherent — reject
    so non-materialized rows can't carry an orphan source_query."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Seed a remote-mode row.
    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "live_orphan",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "query_mode": "remote",
        },
        headers=_auth(token),
    )
    assert r.status_code in (200, 202), r.json()  # synchronous or async
    table_id = r.json()["id"]

    r2 = c.put(
        f"/api/admin/registry/{table_id}",
        json={"source_query": "SELECT 1"},
        headers=_auth(token),
    )
    assert 400 <= r2.status_code < 500, r2.json()


def test_update_materialized_to_remote_clears_source_query(seeded_app, bq_instance):
    """When admin switches a materialized table to remote/local, the stale
    source_query must be cleared in the DB — otherwise the registry shows
    a non-materialized row carrying an orphan SQL body."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Seed a materialized table with a source_query.
    r = c.post(
        "/api/admin/register-table",
        json=_materialized_payload(name="switcher"),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    # Switch to remote — must include bucket+source_table for the new mode
    # (the merged validator runs the BQ payload check on the merged record).
    r2 = c.put(
        f"/api/admin/registry/{table_id}",
        json={
            "query_mode": "remote",
            "bucket": "analytics",
            "source_table": "orders_90d",
        },
        headers=_auth(token),
    )
    assert r2.status_code == 200, r2.json()

    # Verify in the registry: query_mode flipped, source_query cleared.
    r3 = c.get("/api/admin/registry", headers=_auth(token))
    assert r3.status_code == 200, r3.json()
    row = next((t for t in r3.json()["tables"] if t["id"] == table_id), None)
    assert row is not None, f"Table {table_id} not found in registry"
    assert row["query_mode"] == "remote"
    assert row["source_query"] in (None, "")


def test_register_materialized_persists_source_query_in_registry(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_materialized_payload(
            name="persist_q",
            source_query="SELECT col FROM `prj.ds.t` WHERE x = 1",
        ),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    r2 = c.get("/api/admin/registry", headers=_auth(token))
    row = next((t for t in r2.json()["tables"] if t["id"] == table_id), None)
    assert row is not None
    assert row["query_mode"] == "materialized"
    assert "WHERE x = 1" in row["source_query"]
