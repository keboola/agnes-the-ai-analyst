"""Regression guard for PUT field preservation.

Locks the Pydantic semantics that the Phase F form-cleanup relies on:
when the Edit modal omits a field from its payload, the existing value
must survive. If a future maintainer flips ``model_dump()`` to
``exclude_unset=True`` or otherwise changes the partial-update semantics,
these tests fire before partitioned rows or primary keys silently
regress.
"""
import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_put_preserves_omitted_sync_strategy(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = _auth(token)

    r = c.post("/api/admin/register-table", headers=auth, json={
        "name": "events_partitioned",
        "source_type": "keboola",
        "bucket": "in.c-events",
        "source_table": "events",
        "query_mode": "local",
        "sync_strategy": "partitioned",
    })
    assert r.status_code == 201, r.text

    r = c.put("/api/admin/registry/events_partitioned", headers=auth, json={
        "sync_schedule": "daily 03:00",
        "description": "now daily",
    })
    assert r.status_code == 200

    r = c.get("/api/admin/registry", headers=auth)
    rows = r.json()["tables"]
    row = next(t for t in rows if t["id"] == "events_partitioned")
    assert row["sync_strategy"] == "partitioned"


def test_put_preserves_omitted_primary_key(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = _auth(token)

    r = c.post("/api/admin/register-table", headers=auth, json={
        "name": "orders_with_pk",
        "source_type": "keboola",
        "bucket": "in.c-shop",
        "source_table": "orders",
        "query_mode": "local",
        "primary_key": ["order_id", "tenant_id"],
    })
    assert r.status_code == 201, r.text

    r = c.put("/api/admin/registry/orders_with_pk", headers=auth, json={
        "description": "shop orders",
    })
    assert r.status_code == 200

    r = c.get("/api/admin/registry", headers=auth)
    rows = r.json()["tables"]
    row = next(t for t in rows if t["id"] == "orders_with_pk")
    assert row["primary_key"] == ["order_id", "tenant_id"]
