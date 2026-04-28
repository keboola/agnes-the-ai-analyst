# tests/test_v2_sample.py
import importlib
import pytest


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed(conn, *, is_public=True):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
        is_public=is_public,
    )


class TestSampleEndpoint:
    def test_returns_n_rows_for_bq_table(self, reload_db, monkeypatch):
        from app.api import v2_sample
        monkeypatch.setattr(
            v2_sample, "_fetch_bq_sample",
            lambda project, dataset, table, n: [
                {"event_date": "2026-04-27", "country_code": "CZ"},
                {"event_date": "2026-04-26", "country_code": "SK"},
            ],
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "bq_view", n=2, project_id="proj")
        finally:
            conn.close()
        assert data["table_id"] == "bq_view"
        assert len(data["rows"]) == 2

    def test_caps_n_at_100(self, reload_db, monkeypatch):
        from app.api import v2_sample
        captured = {}
        def fake_fetch(project, dataset, table, n):
            captured["n"] = n
            return []
        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", fake_fetch)
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            v2_sample.build_sample(conn, user, "bq_view", n=999, project_id="proj")
        finally:
            conn.close()
        assert captured["n"] == 100

    def test_rbac_check_runs_before_cache(self, reload_db, monkeypatch):
        """Regression: cache check used to come before RBAC, leaking sample rows
        cached by an authorized user to subsequent unauthorized callers."""
        from app.api import v2_sample
        monkeypatch.setattr(
            v2_sample, "_fetch_bq_sample",
            lambda *a, **kw: [{"col": "secret"}],
        )
        monkeypatch.setattr(
            "app.api.v2_sample.can_access_table",
            lambda user, tid, conn: False,
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn, is_public=False)
            admin = {"role": "admin", "email": "admin@x.com"}
            v2_sample.build_sample(conn, admin, "bq_view", n=2, project_id="p")
            other = {"role": "viewer", "email": "viewer@x.com"}
            with pytest.raises(PermissionError):
                v2_sample.build_sample(conn, other, "bq_view", n=2, project_id="p")
        finally:
            conn.close()
