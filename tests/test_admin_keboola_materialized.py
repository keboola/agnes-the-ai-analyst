"""Tests for Keboola materialized registration."""
import pytest


def test_register_keboola_materialized_accepts_source_query(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_recent",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": "SELECT * FROM kbc.\"in.c-sales\".\"orders\" WHERE date > '2026-01-01'",
            "sync_schedule": "daily 03:00",
        },
    )
    assert r.status_code == 201, r.text


def test_register_keboola_materialized_rejects_missing_source_query(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_recent",
            "source_type": "keboola",
            "query_mode": "materialized",
            # source_query missing
        },
    )
    assert r.status_code == 422
    assert "source_query" in r.text


def test_register_keboola_materialized_skips_bucket_check(seeded_app):
    """Materialized rows don't need bucket/source_table — the SELECT inlines
    the references. Mirror of BQ materialized validator behavior."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "x",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": "SELECT 1",
            # No bucket / source_table — must still succeed.
        },
    )
    assert r.status_code == 201, r.text


def test_update_keboola_materialized_clears_stale_source_query_on_mode_switch(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Register materialized.
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "x",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": "SELECT 1",
        },
    )
    assert r.status_code == 201

    # PUT to switch back to local — source_query must clear.
    r = c.put(
        "/api/admin/registry/x",
        headers=auth,
        json={
            "source_type": "keboola",
            "query_mode": "local",
            "bucket": "in.c-foo",
            "source_table": "y",
        },
    )
    assert r.status_code == 200

    r = c.get("/api/admin/registry", headers=auth)
    rows = r.json()["tables"]
    row = next(t for t in rows if t["id"] == "x")
    assert row.get("source_query") in (None, "")
