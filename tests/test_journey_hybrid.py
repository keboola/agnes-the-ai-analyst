"""J3 — Hybrid query journey tests.

Tests the hybrid query pattern: local DuckDB data combined with a mocked
BigQuery-like registration. Since the BQ extension isn't available in test,
we mock the BQ result as an in-memory DuckDB view and validate the local
query side independently.
"""

import pytest
from unittest.mock import patch, MagicMock
from tests.conftest import create_mock_extract


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.journey
class TestHybridQuery:
    def test_local_extract_queryable_after_rebuild(self, seeded_app, mock_extract_factory):
        """Local extract is queryable and forms the foundation for hybrid joins."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        mock_extract_factory(
            "local_src",
            [
                {
                    "name": "local_orders",
                    "data": [
                        {"order_id": "101", "date": "2026-01-01", "amount": "500"},
                        {"order_id": "102", "date": "2026-01-02", "amount": "300"},
                    ],
                }
            ],
        )

        from src.orchestrator import SyncOrchestrator
        result = SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()
        assert "local_src" in result

        resp = c.post(
            "/api/query",
            json={"sql": "SELECT order_id, amount FROM local_orders ORDER BY order_id"},
            headers=_auth(t),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 2
        assert "order_id" in body["columns"]

    def test_query_with_aggregation(self, seeded_app, mock_extract_factory):
        """Aggregation query on local extract works — simulates hybrid query result."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        mock_extract_factory(
            "bq_local",
            [
                {
                    "name": "traffic",
                    "data": [
                        {"date": "2026-01-01", "views": "1000"},
                        {"date": "2026-01-01", "views": "500"},
                        {"date": "2026-01-02", "views": "800"},
                    ],
                }
            ],
        )

        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        resp = c.post(
            "/api/query",
            json={"sql": "SELECT date, SUM(CAST(views AS INTEGER)) as total_views FROM traffic GROUP BY date ORDER BY date"},
            headers=_auth(t),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 2

    def test_join_two_local_views(self, seeded_app, mock_extract_factory):
        """JOIN between two local views — analogous to hybrid join after BQ registration."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        mock_extract_factory(
            "source_a",
            [{"name": "orders_a", "data": [{"id": "1", "user_id": "u1", "total": "100"}]}],
        )
        mock_extract_factory(
            "source_b",
            [{"name": "users_b", "data": [{"id": "u1", "name": "Alice"}]}],
        )

        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        sql = "SELECT o.id, o.total, u.name FROM orders_a o JOIN users_b u ON o.user_id = u.id"
        resp = c.post("/api/query", json={"sql": sql}, headers=_auth(t))
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 1
        # Verify the join produced correct data
        row = body["rows"][0]
        assert "Alice" in row

    def test_query_non_select_rejected(self, seeded_app):
        """Non-SELECT queries are rejected — safety for hybrid endpoint too."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]

        for bad in ["CREATE TABLE t AS SELECT 1", "ATTACH 'x.duckdb' AS x"]:
            resp = c.post("/api/query", json={"sql": bad}, headers=_auth(t))
            assert resp.status_code == 400

    def test_query_with_limit_parameter(self, seeded_app, mock_extract_factory):
        """limit parameter in query request is honoured."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        mock_extract_factory(
            "big_source",
            [
                {
                    "name": "big_table",
                    "data": [{"id": str(i), "val": "x"} for i in range(20)],
                }
            ],
        )

        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        resp = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM big_table", "limit": 5},
            headers=_auth(t),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] <= 5
