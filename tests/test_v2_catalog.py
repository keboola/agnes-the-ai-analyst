import importlib
import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed_two_tables(conn):
    from src.repositories.table_registry import TableRegistryRepository
    repo = TableRegistryRepository(conn)
    repo.register(
        id="orders", name="orders", source_type="keboola",
        bucket="sales", source_table="orders", query_mode="local",
        is_public=True,
    )
    repo.register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=True,
    )


class TestCatalogShape:
    def test_admin_sees_both_tables(self, reload_db):
        from app.api.v2_catalog import build_catalog
        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            admin = {"role": "admin", "email": "a@x.com"}
            data = build_catalog(conn, admin)
            ids = {t["id"] for t in data["tables"]}
            assert {"orders", "bq_view"} <= ids
        finally:
            conn.close()

    def test_local_table_has_duckdb_flavor(self, reload_db):
        from app.api.v2_catalog import build_catalog
        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            admin = {"role": "admin", "email": "a@x.com"}
            data = build_catalog(conn, admin)
            row = next(t for t in data["tables"] if t["id"] == "orders")
            assert row["sql_flavor"] == "duckdb"
            assert row["query_mode"] == "local"
        finally:
            conn.close()

    def test_bq_table_has_bigquery_flavor(self, reload_db):
        from app.api.v2_catalog import build_catalog
        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            admin = {"role": "admin", "email": "a@x.com"}
            data = build_catalog(conn, admin)
            row = next(t for t in data["tables"] if t["id"] == "bq_view")
            assert row["sql_flavor"] == "bigquery"
            assert row["query_mode"] == "remote"
            assert "where_examples" in row
            assert "fetch_via" in row
        finally:
            conn.close()


class TestCatalogCacheRbac:
    """Regression: the per-user payload cache used to leave revoked users
    seeing tables for up to TTL. Cache the underlying rows globally; enforce
    RBAC fresh per request. Same pattern as v2_schema.py / v2_sample.py."""

    def test_rbac_decision_is_fresh_per_call_not_cached(self, reload_db, monkeypatch):
        from app.api import v2_catalog

        conn = reload_db.get_system_db()
        try:
            _seed_two_tables(conn)
            user = {"role": "analyst", "email": "u@x.com"}

            # First call: a fake can_access_table that grants both tables.
            calls = []

            def grant_all(_user, table_id, _conn):
                calls.append(("grant", table_id))
                return True

            monkeypatch.setattr(v2_catalog, "can_access_table", grant_all)
            data1 = v2_catalog.build_catalog(conn, user)
            ids1 = {t["id"] for t in data1["tables"]}
            assert {"orders", "bq_view"} <= ids1

            # Second call (cache HIT on raw rows): can_access_table now denies
            # `orders`. The user must NOT see it any more — RBAC re-evaluates.
            def deny_orders(_user, table_id, _conn):
                calls.append(("eval", table_id))
                return table_id != "orders"

            monkeypatch.setattr(v2_catalog, "can_access_table", deny_orders)
            data2 = v2_catalog.build_catalog(conn, user)
            ids2 = {t["id"] for t in data2["tables"]}
            assert "orders" not in ids2, \
                f"revoked table 'orders' still visible — cache leaked stale RBAC: {ids2}"
            assert "bq_view" in ids2

            # And RBAC ran on the second call (the eval calls are present).
            assert any(kind == "eval" for kind, _ in calls), \
                "RBAC was not re-evaluated on cached call"
        finally:
            conn.close()
            v2_catalog._table_rows_cache.clear() if hasattr(
                v2_catalog._table_rows_cache, "clear"
            ) else None
