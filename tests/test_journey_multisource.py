"""J8 — Multi-source journey tests.

Creates multiple mock extracts from different sources, rebuilds the
orchestrator, and verifies that views from all sources are queryable
and visible in catalog/manifest.
"""

import pytest
from tests.conftest import create_mock_extract


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.journey
class TestMultisourceJourney:
    def test_two_sources_both_queryable(self, seeded_app, mock_extract_factory):
        """Two separate source extracts are both available after rebuild."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        # Source 1: CRM data
        mock_extract_factory(
            "crm_source",
            [
                {
                    "name": "crm_customers",
                    "data": [
                        {"id": "c1", "name": "Alice", "plan": "Pro"},
                        {"id": "c2", "name": "Bob", "plan": "Free"},
                    ],
                }
            ],
        )

        # Source 2: Finance data
        mock_extract_factory(
            "finance_source",
            [
                {
                    "name": "finance_invoices",
                    "data": [
                        {"id": "inv1", "customer_id": "c1", "amount": "500"},
                        {"id": "inv2", "customer_id": "c2", "amount": "50"},
                    ],
                }
            ],
        )

        # Rebuild
        from src.orchestrator import SyncOrchestrator
        result = SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()
        assert "crm_source" in result
        assert "finance_source" in result

        # Query source 1
        resp = c.post(
            "/api/query",
            json={"sql": "SELECT id, name FROM crm_customers ORDER BY id"},
            headers=_auth(t),
        )
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 2

        # Query source 2
        resp = c.post(
            "/api/query",
            json={"sql": "SELECT id, amount FROM finance_invoices ORDER BY id"},
            headers=_auth(t),
        )
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 2

    def test_multisource_manifest_shows_all_tables(self, seeded_app, mock_extract_factory):
        """Manifest reflects tables from both sources."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        mock_extract_factory(
            "src_alpha",
            [{"name": "alpha_events", "data": [{"id": "1", "type": "click"}]}],
        )
        mock_extract_factory(
            "src_beta",
            [{"name": "beta_metrics", "data": [{"id": "1", "value": "42"}]}],
        )

        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        resp = c.get("/api/sync/manifest", headers=_auth(t))
        assert resp.status_code == 200
        tables = resp.json()["tables"]
        assert "alpha_events" in tables
        assert "beta_metrics" in tables

    def test_multisource_join_across_sources(self, seeded_app, mock_extract_factory):
        """Can JOIN views from two different source extracts."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        mock_extract_factory(
            "users_src",
            [{"name": "users", "data": [{"user_id": "u1", "username": "alice"}]}],
        )
        mock_extract_factory(
            "orders_src",
            [{"name": "purchases", "data": [{"order_id": "o1", "user_id": "u1", "total": "99"}]}],
        )

        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        sql = "SELECT u.username, p.total FROM users u JOIN purchases p ON u.user_id = p.user_id"
        resp = c.post("/api/query", json={"sql": sql}, headers=_auth(t))
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 1
        row = body["rows"][0]
        assert "alice" in row
        assert "99" in row

    def test_three_sources_catalog_count(self, seeded_app, mock_extract_factory):
        """Three registered sources produce correct table counts in catalog."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        for src, tbl in [("src1", "t1"), ("src2", "t2"), ("src3", "t3")]:
            mock_extract_factory(
                src,
                [{"name": tbl, "data": [{"id": "1", "v": "x"}]}],
            )
            c.post(
                "/api/admin/register-table",
                json={"name": tbl, "source_type": "keboola", "query_mode": "local"},
                headers=_auth(t),
            )

        from src.orchestrator import SyncOrchestrator
        result = SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()
        # All three sources attached
        assert len(result) == 3

        # Catalog lists all three registered tables
        resp = c.get("/api/catalog/tables", headers=_auth(t))
        assert resp.status_code == 200
        names = {tbl["name"] for tbl in resp.json()["tables"]}
        assert {"t1", "t2", "t3"}.issubset(names)

    def test_source_update_reflects_after_rebuild(self, seeded_app, mock_extract_factory):
        """Updating a source extract and rebuilding shows new row counts."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        # Initial extract with 1 row
        mock_extract_factory(
            "updatable_src",
            [{"name": "live_data", "data": [{"id": "1", "val": "initial"}]}],
        )

        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        resp = c.get("/api/sync/manifest", headers=_auth(t))
        assert resp.json()["tables"].get("live_data", {}).get("rows") == 1

        # Overwrite extract with 3 rows
        mock_extract_factory(
            "updatable_src",
            [
                {
                    "name": "live_data",
                    "data": [
                        {"id": "1", "val": "updated_a"},
                        {"id": "2", "val": "updated_b"},
                        {"id": "3", "val": "updated_c"},
                    ],
                }
            ],
        )
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        resp = c.get("/api/sync/manifest", headers=_auth(t))
        assert resp.json()["tables"]["live_data"]["rows"] == 3
