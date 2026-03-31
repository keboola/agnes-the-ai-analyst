"""E2E tests — extractor + orchestrator pipeline."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import duckdb
import pytest


class TestKeboolaExtractToQuery:
    """Keboola extractor -> extract.duckdb -> orchestrator -> queryable views."""

    def test_full_pipeline(self, e2e_env):
        env = e2e_env

        # 1. Register table in registry
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        conn = get_system_db()
        repo = TableRegistryRepository(conn)
        repo.register(id="orders", name="orders", source_type="keboola",
                      bucket="in.c-crm", source_table="orders", query_mode="local")
        tables = repo.list_by_source("keboola")
        conn.close()

        # 2. Run extractor (mock the DuckDB extension)
        from connectors.keboola.extractor import run as keboola_run

        def mock_legacy(tc, pq_path, keboola_url, keboola_token):
            local = duckdb.connect()
            local.execute(
                f"COPY (SELECT '1' AS id, 'Widget' AS product, '99.99' AS price "
                f"UNION ALL SELECT '2', 'Gadget', '49.99') TO '{pq_path}' (FORMAT PARQUET)"
            )
            local.close()

        output = str(env["extracts_dir"] / "keboola")
        with patch("connectors.keboola.extractor._try_attach_extension", return_value=False), \
             patch("connectors.keboola.extractor._extract_via_legacy", side_effect=mock_legacy):
            result = keboola_run(output, tables, "https://example.com", "fake-token")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 0

        # 3. Verify extract.duckdb
        ext_conn = duckdb.connect(str(env["extracts_dir"] / "keboola" / "extract.duckdb"))
        meta = ext_conn.execute("SELECT table_name, rows, query_mode FROM _meta").fetchall()
        assert len(meta) == 1
        assert meta[0][0] == "orders"
        assert meta[0][1] == 2
        ext_conn.close()

        # 4. Run orchestrator
        from src.orchestrator import SyncOrchestrator
        orch = SyncOrchestrator(analytics_db_path=env["analytics_db"])
        result = orch.rebuild()
        assert "keboola" in result
        assert "orders" in result["keboola"]

        # 5. Verify sync_state updated
        conn2 = get_system_db()
        from src.repositories.sync_state import SyncStateRepository
        state = SyncStateRepository(conn2).get_table_state("orders")
        assert state is not None
        assert state["rows"] == 2
        conn2.close()

        # 6. Verify data queryable via extract.duckdb
        ext_conn2 = duckdb.connect(str(env["extracts_dir"] / "keboola" / "extract.duckdb"))
        rows = ext_conn2.execute("SELECT product FROM orders ORDER BY id").fetchall()
        assert rows[0][0] == "Widget"
        assert rows[1][0] == "Gadget"
        ext_conn2.close()


class TestBigQueryRemoteExtract:
    """BigQuery extractor -- remote only, no data download."""

    def test_remote_only_pipeline(self, e2e_env):
        env = e2e_env
        output = str(env["extracts_dir"] / "bigquery")

        table_configs = [
            {"name": "page_views", "bucket": "analytics", "source_table": "page_views", "description": "Web traffic"},
            {"name": "sessions", "bucket": "analytics", "source_table": "sessions", "description": "User sessions"},
        ]

        from connectors.bigquery import extractor as bq_mod

        # Build extract.duckdb manually to simulate what the BQ extractor would produce,
        # since the real DuckDB BigQuery extension is not available in test environments.
        output_path = Path(output)
        output_path.mkdir(parents=True, exist_ok=True)
        db_path = output_path / "extract.duckdb"

        conn = duckdb.connect(str(db_path))
        bq_mod._create_meta_table(conn)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for tc in table_configs:
            name = tc["name"]
            # Create a placeholder table (no actual BQ data)
            conn.execute(f'CREATE OR REPLACE TABLE "{name}" (dummy INTEGER)')
            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, 0, 0, ?, 'remote')",
                [name, tc.get("description", ""), now],
            )
        conn.close()

        # Verify _meta
        conn = duckdb.connect(str(db_path))
        meta = conn.execute("SELECT table_name, query_mode FROM _meta ORDER BY table_name").fetchall()
        assert len(meta) == 2
        assert all(m[1] == "remote" for m in meta)
        conn.close()

        # Verify no parquet files
        data_dir = env["extracts_dir"] / "bigquery" / "data"
        assert not data_dir.exists() or not list(data_dir.glob("*.parquet"))

        # Verify orchestrator picks up remote tables
        from src.orchestrator import SyncOrchestrator
        orch = SyncOrchestrator(analytics_db_path=env["analytics_db"])
        result = orch.rebuild()
        assert "bigquery" in result
        assert "page_views" in result["bigquery"]
        assert "sessions" in result["bigquery"]


class TestJiraWebhookToQuery:
    """Jira webhook -> incremental parquet -> extract.duckdb -> query."""

    def test_jira_incremental_flow(self, e2e_env):
        env = e2e_env
        jira_dir = env["extracts_dir"] / "jira"

        # 1. Init Jira extract
        from connectors.jira.extract_init import init_extract, update_meta
        init_extract(jira_dir)

        # 2. Simulate incremental_transform: write a parquet to data/issues/
        issues_dir = jira_dir / "data" / "issues"
        pq_path = str(issues_dir / "2026-03.parquet")
        tmp = duckdb.connect()
        tmp.execute(
            f"COPY (SELECT 'PROJ-1' AS issue_key, 'Bug' AS type, 'Fix login' AS summary) "
            f"TO '{pq_path}' (FORMAT PARQUET)"
        )
        tmp.close()

        # 3. Update _meta
        update_meta(jira_dir, "issues")

        # 4. Verify _meta updated
        conn = duckdb.connect(str(jira_dir / "extract.duckdb"))
        meta = conn.execute("SELECT rows FROM _meta WHERE table_name='issues'").fetchone()
        assert meta[0] == 1

        # 5. Verify view works
        row = conn.execute("SELECT issue_key, summary FROM issues").fetchone()
        assert row[0] == "PROJ-1"
        assert row[1] == "Fix login"
        conn.close()

        # 6. Orchestrator picks it up
        from src.orchestrator import SyncOrchestrator
        orch = SyncOrchestrator(analytics_db_path=env["analytics_db"])
        result = orch.rebuild()
        assert "jira" in result
        assert "issues" in result["jira"]


class TestMultiSourceOrchestration:
    """Multiple sources -> single analytics.duckdb."""

    def test_three_sources(self, e2e_env):
        env = e2e_env
        from tests.conftest import create_mock_extract

        # Keboola: 2 tables
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [{"id": "1", "total": "100"}]},
            {"name": "customers", "data": [{"id": "1", "name": "Alice"}]},
        ])

        # Jira: 1 table
        create_mock_extract(env["extracts_dir"], "jira", [
            {"name": "issues", "data": [{"issue_key": "PROJ-1"}]},
        ])

        # Rebuild
        from src.orchestrator import SyncOrchestrator
        orch = SyncOrchestrator(analytics_db_path=env["analytics_db"])
        result = orch.rebuild()

        assert len(result) == 2  # keboola + jira
        total_tables = sum(len(v) for v in result.values())
        assert total_tables == 3  # orders + customers + issues

        # Verify sync_state
        from src.db import get_system_db
        from src.repositories.sync_state import SyncStateRepository
        conn = get_system_db()
        states = SyncStateRepository(conn).get_all_states()
        conn.close()
        table_ids = {s["table_id"] for s in states}
        assert {"orders", "customers", "issues"}.issubset(table_ids)


class TestCorruptExtractHandling:
    """Orchestrator gracefully handles corrupt extract.duckdb."""

    def test_skips_corrupt_continues_valid(self, e2e_env):
        env = e2e_env
        from tests.conftest import create_mock_extract

        # Valid source
        create_mock_extract(env["extracts_dir"], "keboola", [
            {"name": "orders", "data": [{"id": "1"}]},
        ])

        # Corrupt source: write garbage to extract.duckdb
        corrupt_dir = env["extracts_dir"] / "broken"
        corrupt_dir.mkdir()
        (corrupt_dir / "extract.duckdb").write_bytes(b"this is not a duckdb file")

        from src.orchestrator import SyncOrchestrator
        orch = SyncOrchestrator(analytics_db_path=env["analytics_db"])
        result = orch.rebuild()

        # Keboola should work, broken should be skipped
        assert "keboola" in result
        assert "orders" in result["keboola"]
        assert "broken" not in result or result.get("broken") == []


class TestSchemaMigration:
    """Schema v1->v2 migration preserves data and adds new columns."""

    def test_migration_preserves_and_extends(self, e2e_env):
        env = e2e_env

        # Create a v1-style database manually
        db_path = env["data_dir"] / "state" / "system.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP)")
        conn.execute("INSERT INTO schema_version VALUES (1, current_timestamp)")
        conn.execute("""CREATE TABLE table_registry (
            id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, folder VARCHAR,
            sync_strategy VARCHAR, primary_key VARCHAR, description TEXT,
            registered_by VARCHAR, registered_at TIMESTAMP DEFAULT current_timestamp
        )""")
        conn.execute("INSERT INTO table_registry (id, name, folder) VALUES ('old_table', 'Old', 'legacy')")
        # Create minimal required tables
        for tbl in ["users", "sync_state", "sync_history", "user_sync_settings",
                     "knowledge_items", "knowledge_votes", "audit_log", "telegram_links",
                     "pending_codes", "script_registry", "table_profiles", "dataset_permissions"]:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {tbl} (id VARCHAR PRIMARY KEY)")
        conn.close()

        # Open via get_system_db -> triggers migration
        from src.db import get_system_db, get_schema_version
        conn2 = get_system_db()

        assert get_schema_version(conn2) == 3

        # Old data preserved
        old = conn2.execute("SELECT name, folder FROM table_registry WHERE id='old_table'").fetchone()
        assert old[0] == "Old"
        assert old[1] == "legacy"

        # New columns exist and work
        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(conn2)
        repo.register(id="new_table", name="New", source_type="keboola",
                      bucket="in.c-crm", source_table="new", query_mode="local")

        new = repo.get("new_table")
        assert new["source_type"] == "keboola"
        assert new["query_mode"] == "local"

        # Both old and new queryable
        all_tables = repo.list_all()
        assert len(all_tables) == 2
        conn2.close()
