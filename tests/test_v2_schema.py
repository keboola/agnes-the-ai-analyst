# tests/test_v2_schema.py
import importlib
from unittest.mock import patch, MagicMock
import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed_bq_table(conn):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=True,
    )


class TestSchemaEndpoint:
    def test_bq_table_returns_columns_and_dialect_hints(self, reload_db, monkeypatch):
        from app.api import v2_schema
        # Stub the BQ schema fetch to avoid hitting real BQ
        monkeypatch.setattr(
            v2_schema, "_fetch_bq_schema",
            lambda project, dataset, table: [
                {"name": "event_date", "type": "DATE", "nullable": False, "description": ""},
                {"name": "country_code", "type": "STRING", "nullable": True, "description": ""},
            ],
        )
        monkeypatch.setattr(v2_schema, "_fetch_bq_table_options", lambda *a: {"partition_by": "event_date", "clustered_by": []})

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"role": "admin", "email": "a@x.com"}
            data = v2_schema.build_schema(conn, user, "bq_view", project_id="my-proj")
        finally:
            conn.close()
        assert data["table_id"] == "bq_view"
        assert data["sql_flavor"] == "bigquery"
        assert {c["name"] for c in data["columns"]} == {"event_date", "country_code"}
        assert "where_dialect_hints" in data
        assert data["partition_by"] == "event_date"

    def test_unknown_table_raises_404(self, reload_db):
        from app.api.v2_schema import build_schema, NotFound
        conn = reload_db.get_system_db()
        try:
            user = {"role": "admin", "email": "a@x.com"}
            with pytest.raises(NotFound):
                build_schema(conn, user, "missing", project_id="my-proj")
        finally:
            conn.close()
