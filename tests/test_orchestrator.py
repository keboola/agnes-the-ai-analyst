"""Tests for SyncOrchestrator."""

import os
from pathlib import Path

import duckdb
import pytest


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    """Set up DATA_DIR and return paths."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    extracts_dir = tmp_path / "extracts"
    extracts_dir.mkdir()
    analytics_dir = tmp_path / "analytics"
    analytics_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    yield {
        "data_dir": tmp_path,
        "extracts_dir": extracts_dir,
        "analytics_db": str(analytics_dir / "server.duckdb"),
    }


def _create_mock_extract(extracts_dir: Path, source_name: str, tables: list[dict]):
    """Create a mock extract.duckdb with _meta and views."""
    source_dir = extracts_dir / source_name
    source_dir.mkdir()
    data_dir = source_dir / "data"
    data_dir.mkdir()

    db_path = source_dir / "extract.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute(
        """CREATE TABLE _meta (
        table_name VARCHAR, description VARCHAR, rows BIGINT,
        size_bytes BIGINT, extracted_at TIMESTAMP,
        query_mode VARCHAR DEFAULT 'local'
    )"""
    )

    for t in tables:
        name = t["name"]
        rows_data = t.get("data", [])
        query_mode = t.get("query_mode", "local")

        # Create an actual table (simulating what a view on parquet would look like)
        if rows_data:
            cols = ", ".join(f"{k} VARCHAR" for k in rows_data[0].keys())
            conn.execute(f'CREATE TABLE "{name}" ({cols})')
            for row in rows_data:
                vals = ", ".join(f"'{v}'" for v in row.values())
                conn.execute(f'INSERT INTO "{name}" VALUES ({vals})')
        else:
            conn.execute(f'CREATE TABLE "{name}" (id VARCHAR)')

        row_count = len(rows_data)
        conn.execute(
            "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, ?)",
            [name, t.get("description", ""), row_count, 0, query_mode],
        )

    conn.close()


class TestSyncOrchestrator:
    def test_rebuild_empty_extracts(self, setup_env):
        from src.orchestrator import SyncOrchestrator

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()
        assert result == {}

    def test_rebuild_single_source(self, setup_env):
        from src.orchestrator import SyncOrchestrator

        _create_mock_extract(
            setup_env["extracts_dir"],
            "keboola",
            [
                {"name": "orders", "data": [{"id": "1", "total": "100"}]},
                {"name": "customers", "data": [{"id": "1", "name": "Alice"}]},
            ],
        )
        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()
        assert "keboola" in result
        assert set(result["keboola"]) == {"orders", "customers"}

        # Verify views work when source is attached (as the orchestrator leaves it)
        # Open a fresh connection and re-attach to simulate how the analytics DB is used
        conn = duckdb.connect(setup_env["analytics_db"])
        try:
            extract_path = setup_env["extracts_dir"] / "keboola" / "extract.duckdb"
            conn.execute(f"ATTACH '{extract_path}' AS keboola (READ_ONLY)")
            row = conn.execute("SELECT total FROM orders WHERE id='1'").fetchone()
            assert row[0] == "100"
        finally:
            conn.close()

    def test_rebuild_multiple_sources(self, setup_env):
        from src.orchestrator import SyncOrchestrator

        _create_mock_extract(
            setup_env["extracts_dir"],
            "keboola",
            [{"name": "orders", "data": [{"id": "1"}]}],
        )
        _create_mock_extract(
            setup_env["extracts_dir"],
            "jira",
            [{"name": "issues", "data": [{"key": "PROJ-1"}]}],
        )
        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()
        assert "keboola" in result
        assert "jira" in result

    def test_rebuild_skips_missing_extract_db(self, setup_env):
        from src.orchestrator import SyncOrchestrator

        # Create directory without extract.duckdb
        (setup_env["extracts_dir"] / "broken").mkdir()
        _create_mock_extract(
            setup_env["extracts_dir"],
            "keboola",
            [{"name": "orders", "data": [{"id": "1"}]}],
        )
        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()
        assert "broken" not in result
        assert "keboola" in result

    def test_rebuild_source_single(self, setup_env):
        from src.orchestrator import SyncOrchestrator

        _create_mock_extract(
            setup_env["extracts_dir"],
            "jira",
            [{"name": "issues", "data": [{"key": "PROJ-1"}]}],
        )
        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        tables = orch.rebuild_source("jira")
        assert "issues" in tables

    def test_rebuild_source_nonexistent(self, setup_env):
        from src.orchestrator import SyncOrchestrator

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        tables = orch.rebuild_source("nonexistent")
        assert tables == []

    def test_rebuild_with_remote_tables(self, setup_env):
        from src.orchestrator import SyncOrchestrator

        _create_mock_extract(
            setup_env["extracts_dir"],
            "bigquery",
            [
                {
                    "name": "page_views",
                    "query_mode": "remote",
                    "data": [{"url": "/home"}],
                }
            ],
        )
        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()
        assert "bigquery" in result
        assert "page_views" in result["bigquery"]

    def test_rebuild_reads_remote_attach_table(self, setup_env):
        """Orchestrator reads _remote_attach and attempts to ATTACH the extension."""
        from unittest.mock import patch
        from src.orchestrator import SyncOrchestrator

        # Create extract.duckdb with _remote_attach + a local table
        source_dir = setup_env["extracts_dir"] / "keboola"
        source_dir.mkdir()
        (source_dir / "data").mkdir()

        db_path = source_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'local'
        )""")
        conn.execute("""CREATE TABLE _remote_attach (
            alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR
        )""")
        conn.execute(
            "INSERT INTO _remote_attach VALUES ('kbc', 'keboola', 'https://kbc.example.com', 'KEBOOLA_STORAGE_TOKEN')"
        )
        # Local table (has data, works without extension)
        conn.execute('CREATE TABLE "orders" (id VARCHAR)')
        conn.execute("INSERT INTO orders VALUES ('1')")
        conn.execute(
            "INSERT INTO _meta VALUES ('orders', '', 1, 0, current_timestamp, 'local')"
        )
        conn.close()

        # Token env is set but extension install will fail (not available in test)
        # — orchestrator should log warning and continue with local tables
        with patch.dict(os.environ, {"KEBOOLA_STORAGE_TOKEN": "test-token"}):
            orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
            result = orch.rebuild()

        assert "keboola" in result
        assert "orders" in result["keboola"]

    def test_rebuild_remote_attach_skips_missing_token(self, setup_env):
        """Orchestrator skips remote ATTACH when env var is not set."""
        from src.orchestrator import SyncOrchestrator

        source_dir = setup_env["extracts_dir"] / "keboola"
        source_dir.mkdir()
        (source_dir / "data").mkdir()

        db_path = source_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'local'
        )""")
        conn.execute("""CREATE TABLE _remote_attach (
            alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR
        )""")
        conn.execute(
            "INSERT INTO _remote_attach VALUES ('kbc', 'keboola', 'https://kbc.example.com', 'NONEXISTENT_TOKEN_VAR')"
        )
        conn.execute('CREATE TABLE "orders" (id VARCHAR)')
        conn.execute(
            "INSERT INTO _meta VALUES ('orders', '', 0, 0, current_timestamp, 'local')"
        )
        conn.close()

        # No token env set — remote attach should be skipped, local tables still work
        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        assert "keboola" in result
        assert "orders" in result["keboola"]

    def test_rebuild_source_preserves_other_sources(self, setup_env):
        """rebuild_source('jira') must not destroy views from keboola or other sources."""
        from src.orchestrator import SyncOrchestrator

        _create_mock_extract(
            setup_env["extracts_dir"],
            "keboola",
            [{"name": "orders", "data": [{"id": "1", "total": "100"}]}],
        )
        _create_mock_extract(
            setup_env["extracts_dir"],
            "jira",
            [{"name": "issues", "data": [{"key": "PROJ-1"}]}],
        )

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])

        # Full rebuild — both sources visible
        result = orch.rebuild()
        assert "keboola" in result
        assert "jira" in result

        # Jira webhook triggers rebuild_source("jira")
        tables = orch.rebuild_source("jira")
        assert "issues" in tables

        # Full rebuild again (simulates next scheduled run) — keboola must still be there
        result2 = orch.rebuild()
        assert "keboola" in result2, "keboola must survive after rebuild_source('jira')"
        assert "jira" in result2

    def test_rebuild_idempotent(self, setup_env):
        from src.orchestrator import SyncOrchestrator

        _create_mock_extract(
            setup_env["extracts_dir"],
            "keboola",
            [{"name": "orders", "data": [{"id": "1"}]}],
        )
        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result1 = orch.rebuild()
        result2 = orch.rebuild()
        assert result1 == result2

    def test_rejects_malicious_source_name(self, setup_env):
        """Directory names with SQL injection chars must be skipped entirely."""
        from src.orchestrator import SyncOrchestrator

        # Create a directory whose name contains SQL injection characters
        malicious_name = "evil; DROP TABLE users--"
        malicious_dir = setup_env["extracts_dir"] / malicious_name
        malicious_dir.mkdir()
        (malicious_dir / "data").mkdir()

        # Create a valid extract.duckdb inside the malicious directory
        db_path = malicious_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute(
            """CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'local'
        )"""
        )
        conn.execute('CREATE TABLE "orders" (id VARCHAR)')
        conn.execute(
            "INSERT INTO _meta VALUES ('orders', '', 0, 0, current_timestamp, 'local')"
        )
        conn.close()

        # Also create a safe source to confirm non-malicious sources still work
        _create_mock_extract(
            setup_env["extracts_dir"],
            "keboola",
            [{"name": "orders", "data": [{"id": "1"}]}],
        )

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        # The malicious directory must not appear in results
        assert malicious_name not in result
        # The safe source must still be processed
        assert "keboola" in result

    def test_rebuild_cleans_wal_files(self, setup_env):
        """No .wal files should remain after rebuild completes."""
        from src.orchestrator import SyncOrchestrator

        _create_mock_extract(
            setup_env["extracts_dir"],
            "keboola",
            [{"name": "orders", "data": [{"id": "1", "total": "100"}]}],
        )

        analytics_db = setup_env["analytics_db"]
        orch = SyncOrchestrator(analytics_db_path=analytics_db)

        # Simulate a pre-existing WAL file for the target analytics DB
        wal_path = Path(analytics_db + ".wal")
        wal_path.write_text("stale wal")
        assert wal_path.exists(), "Pre-condition: WAL file should exist before rebuild"

        orch.rebuild()

        # After rebuild, no WAL files should remain alongside the analytics DB
        assert not wal_path.exists(), "Old WAL file must be removed during atomic swap"
        # Also verify no temp WAL was left behind
        tmp_wal = Path(analytics_db + ".tmp.wal")
        assert not tmp_wal.exists(), "Temp WAL file must be cleaned up"

    def test_rebuild_while_reading(self, setup_env):
        """Rebuild should succeed even while a read-only connection exists."""
        from src.orchestrator import SyncOrchestrator
        import duckdb

        _create_mock_extract(
            setup_env["extracts_dir"], "keboola",
            [{"name": "orders", "data": [{"id": "1"}]}],
        )
        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        orch.rebuild()

        reader = duckdb.connect(setup_env["analytics_db"], read_only=True)
        result = orch.rebuild()
        assert "keboola" in result
        reader.close()

    def test_rejects_malicious_table_name(self, setup_env):
        """Tables with SQL injection names in _meta must be skipped; safe tables still work."""
        from src.orchestrator import SyncOrchestrator

        source_dir = setup_env["extracts_dir"] / "keboola"
        source_dir.mkdir()
        (source_dir / "data").mkdir()

        db_path = source_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute(
            """CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'local'
        )"""
        )

        # Safe table
        conn.execute('CREATE TABLE "orders" (id VARCHAR)')
        conn.execute("INSERT INTO orders VALUES ('1')")
        conn.execute(
            "INSERT INTO _meta VALUES ('orders', '', 1, 0, current_timestamp, 'local')"
        )

        # Malicious table_name in _meta (no actual table needed — validation rejects before access)
        conn.execute(
            "INSERT INTO _meta VALUES ('evil; DROP TABLE users--', '', 0, 0, current_timestamp, 'local')"
        )
        conn.close()

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        assert "keboola" in result
        # Safe table present
        assert "orders" in result["keboola"]
        # Malicious table_name must not appear
        assert "evil; DROP TABLE users--" not in result["keboola"]



# ---------------------------------------------------------------------------
# Orchestrator failure mode tests
# ---------------------------------------------------------------------------


class TestOrchestratorFailureModes:
    """Tests for how the orchestrator handles corrupted or partial extract.duckdb files."""

    def test_corrupted_extract_duckdb_skipped_not_crashed(self, setup_env):
        """A corrupted extract.duckdb should be skipped (with a warning) and
        not crash the orchestrator. Other sources should still be processed."""
        from src.orchestrator import SyncOrchestrator

        # Create a corrupted extract.duckdb
        corrupt_dir = setup_env["extracts_dir"] / "corrupt_source"
        corrupt_dir.mkdir()
        db_path = corrupt_dir / "extract.duckdb"
        db_path.write_bytes(b"this is not a valid duckdb file!!!")

        # Also create a valid source
        _create_mock_extract(
            setup_env["extracts_dir"],
            "keboola",
            [{"name": "orders", "data": [{"id": "1"}]}],
        )

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        # Corrupt source should not appear
        assert "corrupt_source" not in result
        # Valid source should still be processed
        assert "keboola" in result

    def test_empty_extract_duckdb_skipped(self, setup_env):
        """An extract.duckdb with _meta but no rows should be handled gracefully."""
        from src.orchestrator import SyncOrchestrator

        source_dir = setup_env["extracts_dir"] / "empty_source"
        source_dir.mkdir()
        (source_dir / "data").mkdir()

        db_path = source_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'local'
        )""")
        # No rows in _meta
        conn.close()

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        # Empty source appears but with no tables
        assert "empty_source" in result
        assert result["empty_source"] == []

    def test_extract_duckdb_with_only_failed_tables(self, setup_env):
        """An extract.duckdb where all tables have unsafe names should produce
        no views in the analytics DB."""
        from src.orchestrator import SyncOrchestrator

        source_dir = setup_env["extracts_dir"] / "bad_names"
        source_dir.mkdir()
        (source_dir / "data").mkdir()

        db_path = source_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'local'
        )""")
        # All unsafe names
        conn.execute("INSERT INTO _meta VALUES ('bad-name', '', 0, 0, current_timestamp, 'local')")
        conn.execute("INSERT INTO _meta VALUES ('also bad', '', 0, 0, current_timestamp, 'local')")
        conn.close()

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        # Source appears but with no valid tables
        assert "bad_names" in result
        assert result["bad_names"] == []

    def test_mid_write_extract_duckdb_handled_gracefully(self, setup_env):
        """If an extractor is mid-write (tmp file exists but hasn't been
        swapped yet), the orchestrator should not crash."""
        from src.orchestrator import SyncOrchestrator

        source_dir = setup_env["extracts_dir"] / "midwrite"
        source_dir.mkdir()
        (source_dir / "data").mkdir()

        # No extract.duckdb, but a .tmp file exists (mid-write)
        tmp_path = source_dir / "extract.duckdb.tmp"
        tmp_path.write_bytes(b"partial data")

        # Also create a valid source
        _create_mock_extract(
            setup_env["extracts_dir"],
            "keboola",
            [{"name": "orders", "data": [{"id": "1"}]}],
        )

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        # midwrite source should not appear (no extract.duckdb)
        assert "midwrite" not in result
        # Valid source should still work
        assert "keboola" in result

    def test_multiple_corrupted_sources_do_not_block_others(self, setup_env):
        """Multiple corrupted extract.duckdb files should not prevent
        processing of valid sources."""
        from src.orchestrator import SyncOrchestrator

        # Create two corrupted sources
        for name in ["corrupt_a", "corrupt_b"]:
            d = setup_env["extracts_dir"] / name
            d.mkdir()
            (d / "extract.duckdb").write_bytes(b"garbage " + name.encode())

        # Create a valid source
        _create_mock_extract(
            setup_env["extracts_dir"],
            "keboola",
            [{"name": "orders", "data": [{"id": "1"}]}],
        )

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        assert "corrupt_a" not in result
        assert "corrupt_b" not in result
        assert "keboola" in result
