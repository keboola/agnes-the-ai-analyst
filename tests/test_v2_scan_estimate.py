# tests/test_v2_scan_estimate.py
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


class TestBqAccessErrors:
    """Issue #134: structured 502/400 translation on BQ errors in dry-run path."""

    def test_scan_estimate_returns_502_on_bq_forbidden_serviceusage(self, reload_db, monkeypatch):
        """When the BQ dry-run raises Forbidden mentioning serviceusage, the
        endpoint must translate to HTTP 502 with `cross_project_forbidden`
        and a hint that mentions billing_project."""
        from app.api import v2_scan
        from google.api_core.exceptions import Forbidden

        def _raise_forbidden(project, sql):
            raise Forbidden("Permission denied: serviceusage.services.use on project foo")

        monkeypatch.setattr(v2_scan, "_bq_dry_run_bytes", _raise_forbidden)
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )
        # Endpoint reads project/billing_project from instance.yaml; fake both.
        cfg = {
            ("data_source", "bigquery", "project"): "data-proj",
            ("data_source", "bigquery", "billing_project"): "billing-proj",
        }
        monkeypatch.setattr(
            "app.api.v2_scan.get_value",
            lambda *keys, **kw: cfg.get(keys, kw.get("default", "")),
        )

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 1000,
            }
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_scan.scan_estimate_endpoint(raw=req, user=user, conn=conn))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "cross_project_forbidden"
        assert "hint" in detail["details"]
        assert "billing_project" in detail["details"]["hint"].lower()

    def test_scan_estimate_returns_502_on_bq_forbidden_non_serviceusage(self, reload_db, monkeypatch):
        """A Forbidden that is NOT about serviceusage (e.g. dataset-level ACL)
        still becomes a 502, but with `bq_forbidden` and an empty hint."""
        from app.api import v2_scan
        from google.api_core.exceptions import Forbidden

        def _raise_forbidden(project, sql):
            raise Forbidden("Access Denied: Table foo.bar.baz: User does not have permission")

        monkeypatch.setattr(v2_scan, "_bq_dry_run_bytes", _raise_forbidden)
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )
        # Endpoint reads project/billing_project from instance.yaml; fake both.
        cfg = {
            ("data_source", "bigquery", "project"): "data-proj",
            ("data_source", "bigquery", "billing_project"): "billing-proj",
        }
        monkeypatch.setattr(
            "app.api.v2_scan.get_value",
            lambda *keys, **kw: cfg.get(keys, kw.get("default", "")),
        )

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
            }
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_scan.scan_estimate_endpoint(raw=req, user=user, conn=conn))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"] == "bq_forbidden"

    def test_scan_estimate_returns_400_on_bq_bad_request(self, reload_db, monkeypatch):
        """`/scan/estimate` SQL is user-derived (built from req.select/where/order_by),
        so a BQ BadRequest must surface as HTTP 400 with `bq_bad_request`."""
        from app.api import v2_scan
        from google.api_core.exceptions import BadRequest

        def _raise_bad_request(project, sql):
            raise BadRequest("Syntax error: unexpected token at line 1, column 5")

        monkeypatch.setattr(v2_scan, "_bq_dry_run_bytes", _raise_bad_request)
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )
        cfg = {
            ("data_source", "bigquery", "project"): "data-proj",
            ("data_source", "bigquery", "billing_project"): "billing-proj",
        }
        monkeypatch.setattr(
            "app.api.v2_scan.get_value",
            lambda *keys, **kw: cfg.get(keys, kw.get("default", "")),
        )

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"role": "admin", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
            }
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(v2_scan.scan_estimate_endpoint(raw=req, user=user, conn=conn))
        finally:
            conn.close()

        assert exc_info.value.status_code == 400
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "bq_bad_request"
        assert "Syntax error" in detail["message"]
