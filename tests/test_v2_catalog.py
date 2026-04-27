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
