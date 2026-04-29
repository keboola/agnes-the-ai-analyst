# tests/test_v2_sample.py
import asyncio
import importlib
import pytest
from fastapi import HTTPException


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


class TestBqAccessErrors:
    """Issue #134: cross-project Forbidden + billing_project fallback."""

    @pytest.fixture(autouse=True)
    def _clear_sample_cache(self):
        """The sample-result TTL cache is module-level; clear it between
        tests so cached payloads from a sibling test don't mask call paths."""
        from app.api import v2_sample
        v2_sample._sample_cache.clear()
        yield
        v2_sample._sample_cache.clear()

    def test_sample_returns_502_on_bq_forbidden_serviceusage(self, reload_db, monkeypatch):
        """When the BQ extension raises Forbidden mentioning serviceusage,
        the endpoint must translate to HTTP 502 with a structured body
        whose `error` is `cross_project_forbidden` and whose hint mentions
        `billing_project`."""
        from app.api import v2_sample
        from google.api_core.exceptions import Forbidden

        def _raise_forbidden(*args, **kwargs):
            raise Forbidden("Permission denied: serviceusage.services.use on project foo")

        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", _raise_forbidden)

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}

            # Endpoint is async — drive it directly.
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_sample.sample(
                    table_id="bq_view", n=5, user=user, conn=conn,
                ))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "cross_project_forbidden"
        assert "billing_project" in detail["details"]["hint"].lower()

    def test_sample_returns_502_on_bq_forbidden_non_serviceusage(self, reload_db, monkeypatch):
        """A Forbidden that is NOT about serviceusage (e.g. dataset-level ACL)
        still becomes a 502, but with `bq_forbidden` (no billing_project hint)."""
        from app.api import v2_sample
        from google.api_core.exceptions import Forbidden

        def _raise_forbidden(*args, **kwargs):
            raise Forbidden("Access Denied: Table foo.bar.baz: User does not have permission")

        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", _raise_forbidden)

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}

            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_sample.sample(
                    table_id="bq_view", n=5, user=user, conn=conn,
                ))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"] == "bq_forbidden"

    def test_sample_reads_billing_project_from_instance_yaml(self, reload_db, monkeypatch):
        """Regression guard for the original bug: the project passed to
        _fetch_bq_sample must come from billing_project when set, not from
        project."""
        from app.api import v2_sample

        captured = {}

        def _capture(project, dataset, table, n):
            captured["project"] = project
            return []

        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", _capture)

        # Map (data_source, bigquery, <key>) → value; default to "" otherwise.
        cfg = {
            ("data_source", "bigquery", "project"): "data-proj",
            ("data_source", "bigquery", "billing_project"): "billing-proj",
        }
        monkeypatch.setattr(
            "app.api.v2_sample.get_value",
            lambda *keys, **kw: cfg.get(keys, kw.get("default", "")),
        )

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            asyncio.run(v2_sample.sample(
                table_id="bq_view", n=5, user=user, conn=conn,
            ))
        finally:
            conn.close()

        assert captured["project"] == "billing-proj"

    def test_sample_falls_back_to_project_when_billing_project_unset(self, reload_db, monkeypatch):
        """If billing_project is empty, project must still be used (no regression
        for deployments that haven't set billing_project yet)."""
        from app.api import v2_sample

        captured = {}

        def _capture(project, dataset, table, n):
            captured["project"] = project
            return []

        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", _capture)

        cfg = {
            ("data_source", "bigquery", "project"): "data-proj",
            # billing_project intentionally absent → falls through to default
        }
        monkeypatch.setattr(
            "app.api.v2_sample.get_value",
            lambda *keys, **kw: cfg.get(keys, kw.get("default", "")),
        )

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            asyncio.run(v2_sample.sample(
                table_id="bq_view", n=5, user=user, conn=conn,
            ))
        finally:
            conn.close()

        assert captured["project"] == "data-proj"
