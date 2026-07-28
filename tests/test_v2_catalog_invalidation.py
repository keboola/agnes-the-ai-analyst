"""Unified cache flush across the three in-memory catalog/schema/sample
caches on registry write.

Post-0.50: the persistent ``bq_metadata_cache`` is intentionally NOT
invalidated here. That table's lifecycle is owned by the scheduler-
driven refresh — admins who need an immediate refresh after editing a
remote row hit ``POST /api/v2/metadata-cache/refresh?table=<id>``
explicitly. Auto-invalidation on every registry edit would re-introduce
the request-path BQ fan-out the refactor exists to avoid.
"""

from src.db import get_system_db
from src.repositories.bq_metadata_cache import BqMetadataCacheRepository


def test_invalidate_flushes_three_in_memory_caches():
    from app.api import v2_catalog, v2_schema, v2_sample

    # Pre-populate.
    v2_catalog._table_rows_cache.set("all", ["fake_row"])
    v2_schema._schema_cache.set("orders", {"columns": []})
    v2_sample._sample_cache.set("orders|10", [{"row": 1}])

    v2_catalog.invalidate_for_table("orders")

    assert v2_catalog._table_rows_cache.get("all") is None
    assert v2_schema._schema_cache.get("orders") is None
    # Sample cache is cleared whole (we don't have prefix-invalidation).
    assert v2_sample._sample_cache.get("orders|10") is None


def test_invalidate_does_not_touch_persistent_bq_cache():
    """The persistent cache survives registry-row invalidations; only an
    explicit ``POST /api/v2/metadata-cache/refresh`` (or the scheduled
    refresh) should change it."""
    from app.api import v2_catalog

    conn = get_system_db()
    try:
        BqMetadataCacheRepository(conn).upsert_success(
            "survives_invalidate",
            rows=42, size_bytes=4096, partition_by=None, clustered_by=None,
        )
    finally:
        conn.close()

    v2_catalog.invalidate_for_table("survives_invalidate")

    conn = get_system_db()
    try:
        row = BqMetadataCacheRepository(conn).get("survives_invalidate")
    finally:
        conn.close()
    assert row is not None
    assert row["rows"] == 42


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
