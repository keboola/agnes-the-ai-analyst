# tests/test_v2_scan.py
import importlib
from unittest.mock import MagicMock
import pyarrow as pa
import pytest

from app.api.v2_arrow import parse_ipc_bytes


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


class TestScan:
    def test_returns_arrow_ipc_for_simple_request(self, reload_db, monkeypatch):
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )
        fake_table = pa.table(
            {"event_date": ["2026-04-27"], "country_code": ["CZ"]}
        )
        monkeypatch.setattr(
            v2_scan, "_run_bq_scan",
            lambda *a, **kw: fake_table,
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 100,
            }
            tracker = v2_scan._build_quota_tracker()
            ipc_bytes = v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
        finally:
            conn.close()
        got = parse_ipc_bytes(ipc_bytes)
        assert got.num_rows == 1
        assert got.column_names == ["event_date", "country_code"]

    def test_quota_concurrent_exceeded_raises_429(self, reload_db, monkeypatch):
        from app.api import v2_scan
        from app.api.v2_quota import QuotaTracker, QuotaExceededError, KIND_CONCURRENT
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        fake_table = pa.table({"event_date": ["2026-04-27"]})
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda *a, **kw: fake_table)

        tracker = QuotaTracker(max_concurrent_per_user=1, max_daily_bytes_per_user=10**12)
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "limit": 1}

            # Hold one concurrent slot
            with tracker.acquire(user="a@x.com"):
                with pytest.raises(QuotaExceededError) as e:
                    v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
                assert e.value.kind == KIND_CONCURRENT
        finally:
            conn.close()

    def test_validator_rejection_propagates(self, reload_db, monkeypatch):
        from app.api import v2_scan
        from app.api.where_validator import WhereValidationError, REJECT_UNKNOWN_FUNCTION
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )

        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "where": "event_date = NUKE_FN()",
            }
            with pytest.raises(WhereValidationError) as e:
                v2_scan.run_scan(conn, user, req, project_id="proj", quota=tracker)
            assert e.value.kind == REJECT_UNKNOWN_FUNCTION
        finally:
            conn.close()
