"""J2 — Sync & Query journey tests.

Complete flow: register table → create mock extract → rebuild orchestrator →
query data via API → verify catalog listing.
"""

import pytest
from tests.conftest import create_mock_extract


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _keboola_instance(monkeypatch):
    """Keboola instance fixture — required by tests that POST
    `source_type='keboola'` payloads. The register-table source-type
    availability validator refuses Keboola registrations on the default
    unconfigured test instance (where get_data_source_type() returns
    'local')."""
    fake_cfg = {
        "data_source": {
            "type": "keboola",
            "keboola": {
                "stack_url": "https://connection.keboola.com",
                "project_id": "1234",
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config", lambda: fake_cfg, raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    yield
    reset_cache()


@pytest.mark.journey
class TestSyncAndQuery:
    def test_register_create_rebuild_query(self, seeded_app, mock_extract_factory):
        """Full flow: register → mock extract → rebuild → query rows."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        # Step 1: register table
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "orders",
                "source_type": "keboola",
                "bucket": "in.c-crm",
                "source_table": "orders",
                "query_mode": "local",
            },
            headers=_auth(t),
        )
        assert resp.status_code == 201

        # Step 2: create mock extract
        mock_extract_factory(
            "keboola",
            [
                {
                    "name": "orders",
                    "data": [
                        {"id": "1", "product": "Widget", "amount": "100"},
                        {"id": "2", "product": "Gadget", "amount": "200"},
                    ],
                }
            ],
        )

        # Step 3: rebuild orchestrator
        from src.orchestrator import SyncOrchestrator
        result = SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()
        assert "keboola" in result
        assert "orders" in result["keboola"]

        # Step 4: query data
        resp = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM orders ORDER BY id"},
            headers=_auth(t),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 2
        assert "id" in body["columns"]

    def test_catalog_lists_registered_table(self, seeded_app):
        """After registration, table appears in /api/catalog/tables."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]

        c.post(
            "/api/admin/register-table",
            json={"name": "customers", "source_type": "keboola", "query_mode": "local"},
            headers=_auth(t),
        )

        resp = c.get("/api/catalog/tables", headers=_auth(t))
        assert resp.status_code == 200
        names = {tbl["name"] for tbl in resp.json()["tables"]}
        assert "customers" in names

    def test_query_blocked_keywords(self, seeded_app):
        """DROP and other DDL/dangerous statements are blocked."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]

        for bad_sql in [
            "DROP TABLE orders",
            "INSERT INTO orders VALUES (1)",
            "SELECT * FROM read_parquet('/tmp/x.parquet')",
        ]:
            resp = c.post("/api/query", json={"sql": bad_sql}, headers=_auth(t))
            assert resp.status_code == 400, f"Expected 400 for: {bad_sql}"

    def test_manifest_reflects_synced_tables(self, seeded_app, mock_extract_factory):
        """After rebuild, manifest includes synced table with correct row count."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        mock_extract_factory(
            "keboola",
            [
                {
                    "name": "products",
                    "data": [
                        {"id": "1", "name": "Alpha"},
                        {"id": "2", "name": "Beta"},
                        {"id": "3", "name": "Gamma"},
                    ],
                }
            ],
        )

        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        resp = c.get("/api/sync/manifest", headers=_auth(t))
        assert resp.status_code == 200
        tables = resp.json()["tables"]
        assert "products" in tables
        assert tables["products"]["rows"] == 3

    def test_query_empty_result(self, seeded_app, mock_extract_factory):
        """Query against a view with no rows returns empty result set."""
        c = seeded_app["client"]
        t = seeded_app["admin_token"]
        env = seeded_app["env"]

        mock_extract_factory(
            "keboola",
            [{"name": "empty_table", "data": [{"id": "1", "val": "x"}]}],
        )

        from src.orchestrator import SyncOrchestrator
        SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

        resp = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM empty_table WHERE id = 'nonexistent'"},
            headers=_auth(t),
        )
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 0
