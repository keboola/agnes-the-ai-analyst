"""Unified cache flush across all four catalog/schema/sample/metadata
caches on registry write."""

from unittest.mock import patch


def test_invalidate_flushes_all_four_caches():
    from app.api import v2_catalog, v2_schema, v2_sample
    from app.api._metadata_models import TableMetadata

    # Pre-populate.
    v2_catalog._table_rows_cache.set("all", ["fake_row"])
    v2_catalog._metadata_cache.set("orders", TableMetadata(rows=10))
    v2_schema._schema_cache.set("orders", {"columns": []})
    v2_sample._sample_cache.set("orders|10", [{"row": 1}])

    v2_catalog.invalidate_for_table("orders")

    assert v2_catalog._table_rows_cache.get("all") is None
    assert v2_catalog._metadata_cache.get("orders") is None
    assert v2_schema._schema_cache.get("orders") is None
    # Sample cache is cleared whole (we don't have prefix-invalidation).
    assert v2_sample._sample_cache.get("orders|10") is None


def test_invalidate_schedules_single_row_rewarm(monkeypatch):
    """After the flush, a background re-warm task is scheduled for the
    same table_id. Assert via patching create_task."""
    import asyncio
    from app.api import v2_catalog

    scheduled = []

    def fake_create_task(coro):
        # Drain the coroutine so the test doesn't leak it.
        coro.close()
        scheduled.append(coro)
        return None

    # Simulate a running event loop so the create_task branch is reached.
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: object())
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)
    v2_catalog.invalidate_for_table("orders")
    assert len(scheduled) == 1


def test_register_table_invalidates(seeded_app):
    """Registering a table flushes the rows cache so the next catalog
    request reflects it without waiting for the 5-min TTL."""
    from app.api import v2_catalog
    v2_catalog._table_rows_cache.set("all", [])

    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/admin/register-table", json={
        "name": "new_t",
        "source_type": "keboola",
        "bucket": "in.c-x",
        "source_table": "t",
        "query_mode": "local",
    }, headers=headers)
    assert v2_catalog._table_rows_cache.get("all") is None


def test_update_table_invalidates(seeded_app):
    from app.api import v2_catalog
    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    headers = {"Authorization": f"Bearer {token}"}

    client.post("/api/admin/register-table", json={
        "name": "u_t",
        "source_type": "keboola",
        "bucket": "in.c-x",
        "source_table": "t",
        "query_mode": "local",
    }, headers=headers)
    v2_catalog._table_rows_cache.set("all", ["pre-update"])
    client.put("/api/admin/registry/u_t", json={"description": "new"}, headers=headers)
    assert v2_catalog._table_rows_cache.get("all") is None


def test_unregister_table_invalidates(seeded_app):
    from app.api import v2_catalog
    client = seeded_app["client"]
    token = seeded_app["admin_token"]
    headers = {"Authorization": f"Bearer {token}"}

    client.post("/api/admin/register-table", json={
        "name": "d_t",
        "source_type": "keboola",
        "bucket": "in.c-x",
        "source_table": "t",
        "query_mode": "local",
    }, headers=headers)
    v2_catalog._table_rows_cache.set("all", ["pre-delete"])
    client.delete("/api/admin/registry/d_t", headers=headers)
    assert v2_catalog._table_rows_cache.get("all") is None
