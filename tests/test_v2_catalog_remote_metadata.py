"""Catalog endpoint integration: per-table metadata enrichment for
remote rows."""

from unittest.mock import patch

from app.api._metadata_models import TableMetadata


def _register_table(seeded_app, **kwargs):
    """Register a table into the test DB using TableRegistryRepository."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    conn = get_system_db()
    try:
        repo = TableRegistryRepository(conn)
        # `name` defaults to `id` if not supplied
        name = kwargs.pop("name", kwargs.get("id"))
        repo.register(name=name, **kwargs)
    finally:
        conn.close()


def test_remote_row_includes_metadata_fields(seeded_app, monkeypatch):
    """Catalog response for a query_mode='remote' BQ row carries the four
    new fields populated by the provider."""
    # Reset catalog row cache so this test's registered table is visible.
    from app.api import v2_catalog
    v2_catalog._table_rows_cache.clear()
    v2_catalog._metadata_cache.clear()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    fake_meta = TableMetadata(
        rows=10000, size_bytes=2_000_000,
        partition_by="event_date", clustered_by=["country", "platform"],
    )

    _register_table(
        seeded_app,
        id="orders", source_type="bigquery", bucket="dwh_base",
        source_table="orders_2024", query_mode="remote",
    )

    with patch(
        "connectors.bigquery.metadata.fetch", return_value=fake_meta,
    ):
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
    # Existing fields still present.
    assert orders["query_mode"] == "remote"


def test_local_row_unaffected_by_provider_dispatch(seeded_app):
    """query_mode='local' rows take the parquet-stat path; provider not called."""
    from app.api import v2_catalog
    v2_catalog._table_rows_cache.clear()
    v2_catalog._metadata_cache.clear()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    _register_table(
        seeded_app,
        id="users", source_type="keboola", bucket="in.c-crm",
        source_table="users", query_mode="local",
    )

    with patch("connectors.keboola.metadata.fetch") as mock_fetch:
        r = c.get(
            "/api/v2/catalog",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    mock_fetch.assert_not_called()


def test_provider_failure_returns_null_metadata(seeded_app):
    """Provider returns None → row appears with null new fields, not
    a 500. Catalog endpoint must stay 200."""
    from app.api import v2_catalog
    v2_catalog._table_rows_cache.clear()
    v2_catalog._metadata_cache.clear()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    _register_table(
        seeded_app,
        id="broken", source_type="bigquery", bucket="dwh_base",
        source_table="broken_t", query_mode="remote",
    )

    with patch(
        "connectors.bigquery.metadata.fetch", return_value=None,
    ):
        r = c.get(
            "/api/v2/catalog",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    broken = next(t for t in tables if t["id"] == "broken")
    assert broken["rows"] is None
    assert broken["size_bytes"] is None
    assert broken["partition_by"] is None
    assert broken["clustered_by"] is None


def test_cache_hit_does_not_call_provider_twice(seeded_app):
    """First call invokes provider; second within 15 min hits cache."""
    from app.api import v2_catalog
    v2_catalog._table_rows_cache.clear()
    v2_catalog._metadata_cache.clear()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    _register_table(
        seeded_app,
        id="orders", source_type="bigquery", bucket="dwh_base",
        source_table="orders_2024", query_mode="remote",
    )

    fake_meta = TableMetadata(rows=1, size_bytes=2)
    with patch(
        "connectors.bigquery.metadata.fetch", return_value=fake_meta,
    ) as mock_fetch:
        c.get("/api/v2/catalog", headers={"Authorization": f"Bearer {token}"})
        c.get("/api/v2/catalog", headers={"Authorization": f"Bearer {token}"})
    assert mock_fetch.call_count == 1
