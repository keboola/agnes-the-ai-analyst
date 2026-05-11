"""Catalog endpoint integration: per-table metadata enrichment for
remote rows.

Post-0.50 the catalog endpoint reads enrichment fields exclusively from
the persistent ``bq_metadata_cache`` table (populated by the scheduler-
driven refresh in ``app/api/bq_metadata_refresh.py``). These tests
pre-seed cache rows and verify the catalog response shape; they do NOT
mock ``connectors.bigquery.metadata.fetch`` because that path is no
longer reachable from the catalog request.
"""

from unittest.mock import patch


def _register_table(seeded_app, **kwargs):
    """Register a table into the test DB using TableRegistryRepository."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    conn = get_system_db()
    try:
        repo = TableRegistryRepository(conn)
        name = kwargs.pop("name", kwargs.get("id"))
        repo.register(name=name, **kwargs)
    finally:
        conn.close()


def _seed_cache_row(
    table_id: str,
    *,
    rows=None,
    size_bytes=None,
    partition_by=None,
    clustered_by=None,
):
    """Insert a successful refresh row into bq_metadata_cache."""
    from src.db import get_system_db
    from src.repositories.bq_metadata_cache import BqMetadataCacheRepository
    conn = get_system_db()
    try:
        BqMetadataCacheRepository(conn).upsert_success(
            table_id,
            rows=rows,
            size_bytes=size_bytes,
            partition_by=partition_by,
            clustered_by=clustered_by,
        )
    finally:
        conn.close()


def _reset_catalog_caches():
    from app.api import v2_catalog
    v2_catalog._table_rows_cache.clear()


def test_remote_row_includes_metadata_fields(seeded_app):
    """Catalog response for a query_mode='remote' BQ row carries the four
    enrichment fields read from the persistent cache."""
    _reset_catalog_caches()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    _register_table(
        seeded_app,
        id="orders", source_type="bigquery", bucket="dwh_base",
        source_table="orders_2024", query_mode="remote",
    )
    _seed_cache_row(
        "orders",
        rows=10000, size_bytes=2_000_000,
        partition_by="event_date", clustered_by=["country", "platform"],
    )

    r = c.get(
        "/api/v2/catalog",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    orders = next(t for t in tables if t["id"] == "orders")
    assert orders["rows"] == 10000
    assert orders["size_bytes"] == 2_000_000
    assert orders["partition_by"] == "event_date"
    assert orders["clustered_by"] == ["country", "platform"]
    assert orders["query_mode"] == "remote"
    assert orders["metadata_freshness"] == "fresh"


def test_remote_row_with_no_cache_returns_null_fields(seeded_app):
    """Catalog response for a remote row with no cache entry — first boot
    before scheduler tick — returns null enrichment fields and
    metadata_freshness='never_fetched'. MUST stay 200; MUST NOT call BQ."""
    _reset_catalog_caches()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    _register_table(
        seeded_app,
        id="cold_t", source_type="bigquery", bucket="dwh_base",
        source_table="cold_t", query_mode="remote",
    )

    # Patch the BQ provider so we can prove the request path never reaches it.
    with patch("connectors.bigquery.metadata.fetch") as mock_fetch:
        r = c.get(
            "/api/v2/catalog",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    mock_fetch.assert_not_called()

    tables = r.json()["tables"]
    cold = next(t for t in tables if t["id"] == "cold_t")
    assert cold["rows"] is None
    assert cold["size_bytes"] is None
    assert cold["partition_by"] is None
    assert cold["clustered_by"] == []
    assert cold["metadata_freshness"] == "never_fetched"


def test_local_row_metadata_freshness_is_not_applicable(seeded_app):
    """query_mode='local' rows take the parquet-stat path; the freshness
    field signals that the BQ cache concept doesn't apply."""
    _reset_catalog_caches()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    _register_table(
        seeded_app,
        id="users", source_type="keboola", bucket="in.c-crm",
        source_table="users", query_mode="local",
    )

    r = c.get(
        "/api/v2/catalog",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    users = next(t for t in tables if t["id"] == "users")
    assert users["metadata_freshness"] == "not_applicable"


def test_zero_size_bytes_reports_small_not_unknown(seeded_app):
    """Devin Review #1 regression preserved across the refactor: a cache
    row with size_bytes=0 must surface rough_size_hint='small', not None.
    """
    _reset_catalog_caches()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    _register_table(
        seeded_app,
        id="empty_t", source_type="bigquery", bucket="dwh_base",
        source_table="empty_t", query_mode="remote",
    )
    _seed_cache_row("empty_t", rows=0, size_bytes=0, clustered_by=[])

    r = c.get(
        "/api/v2/catalog",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    empty = next(t for t in tables if t["id"] == "empty_t")
    assert empty["size_bytes"] == 0
    assert empty["rough_size_hint"] == "small"


def test_catalog_request_never_calls_bq(seeded_app):
    """The whole point of the refactor: even with a cold cache and a
    remote BQ row in the registry, GET /api/v2/catalog MUST NOT touch
    the BQ provider. Regressing this re-introduces the >90 s hang."""
    _reset_catalog_caches()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    _register_table(
        seeded_app,
        id="orders", source_type="bigquery", bucket="dwh_base",
        source_table="orders_2024", query_mode="remote",
    )

    with patch("connectors.bigquery.metadata.fetch") as mock_fetch:
        c.get("/api/v2/catalog", headers={"Authorization": f"Bearer {token}"})
        c.get("/api/v2/catalog", headers={"Authorization": f"Bearer {token}"})

    mock_fetch.assert_not_called()
