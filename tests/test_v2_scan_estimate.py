# tests/test_v2_scan_estimate.py
import importlib
import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed(conn):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=True,
    )


class TestScanEstimate:
    def test_returns_scan_bytes_for_bq(self, reload_db, monkeypatch):
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_bq_dry_run_bytes",
            lambda project, sql: 4_400_000_000,
        )
        # Stub the schema fetch the validator uses
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 1000000,
            }
            data = v2_scan.estimate(conn, user, req, project_id="proj")
        finally:
            conn.close()
        assert data["estimated_scan_bytes"] == 4_400_000_000
        assert "estimated_result_rows" in data
        assert "bq_cost_estimate_usd" in data
