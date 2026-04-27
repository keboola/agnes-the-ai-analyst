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


class TestBQMetadataAuth:
    """Orchestrator fetches a fresh metadata token for BQ remote attach."""

    def test_bq_extension_triggers_metadata_token_fetch(self, setup_env, monkeypatch):
        """When _remote_attach.extension='bigquery' with empty token_env, orchestrator
        calls get_metadata_token() and creates a DuckDB secret before ATTACH."""
        from src.orchestrator import SyncOrchestrator
        from unittest.mock import MagicMock

        # Build extract.duckdb with bq _remote_attach row
        source_dir = setup_env["extracts_dir"] / "bigquery"
        source_dir.mkdir()
        db_path = source_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR DEFAULT 'remote'
        )""")
        conn.execute("""CREATE TABLE _remote_attach (
            alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR
        )""")
        conn.execute(
            "INSERT INTO _remote_attach VALUES ('bq', 'bigquery', 'project=test-proj', '')"
        )
        # Local stub view so rebuild has something to attach (avoids INSTALL bigquery in test)
        conn.execute('CREATE TABLE "stub" (x INT)')
        conn.execute("INSERT INTO stub VALUES (1)")
        conn.execute(
            "INSERT INTO _meta VALUES ('stub', '', 1, 0, current_timestamp, 'local')"
        )
        conn.close()

        # Stub get_metadata_token
        called = {"count": 0}
        def fake_token():
            called["count"] += 1
            return "ya29.fake-token"
        monkeypatch.setattr(
            "src.orchestrator.get_metadata_token",
            fake_token,
        )

        # Capture executed SQL on the master connection. DuckDB's PyConnection has
        # read-only attributes, so wrap it in a proxy instead of patching `.execute`.
        captured = []

        class _ConnProxy:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args, **kwargs):
                captured.append(sql)
                up = sql.upper()
                # Skip BQ-extension-specific calls — they need real BQ network access
                # that isn't available in unit tests. Match on `TYPE bigquery`
                # rather than substring "bigquery" so we don't shadow ATTACH of
                # extract.duckdb files that live under /extracts/bigquery/.
                if "INSTALL BIGQUERY" in up or "LOAD BIGQUERY" in up:
                    return MagicMock()
                if "CREATE OR REPLACE SECRET" in up and "TYPE BIGQUERY" in up:
                    return MagicMock()
                if up.startswith("ATTACH ") and "TYPE BIGQUERY" in up:
                    return MagicMock()
                return self._inner.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        real_connect = duckdb.connect

        def spy_connect(path, *a, **kw):
            return _ConnProxy(real_connect(path, *a, **kw))

        monkeypatch.setattr("src.orchestrator.duckdb.connect", spy_connect)

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        orch.rebuild()

        assert called["count"] >= 1, "get_metadata_token() must be called for BQ source"
        assert any(
            "CREATE OR REPLACE SECRET" in s.upper() and "TYPE BIGQUERY" in s.upper()
            for s in captured
        ), "orchestrator must create DuckDB secret with metadata token"
        # ATTACH for BQ must not include TOKEN= clause (auth is via the secret)
        attach_for_bq = [
            s for s in captured
            if s.upper().startswith("ATTACH ") and "TYPE BIGQUERY" in s.upper()
        ]
        assert attach_for_bq, "expected ATTACH for the bq alias"
        assert all("TOKEN '" not in s for s in attach_for_bq), \
            f"ATTACH for BQ must not pass TOKEN= directly (auth via secret); got: {attach_for_bq}"

    def test_bq_metadata_failure_logs_and_skips(self, setup_env, monkeypatch, caplog):
        """If metadata is unreachable, orchestrator logs and skips the BQ source — does not crash."""
        from src.orchestrator import SyncOrchestrator
        from connectors.bigquery.auth import BQMetadataAuthError
        import logging

        # Build minimal BQ extract with a co-located local 'stub' table
        source_dir = setup_env["extracts_dir"] / "bigquery"
        source_dir.mkdir()
        db_path = source_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR DEFAULT 'remote'
        )""")
        conn.execute("""CREATE TABLE _remote_attach (
            alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR
        )""")
        conn.execute(
            "INSERT INTO _remote_attach VALUES ('bq', 'bigquery', 'project=test-proj', '')"
        )
        conn.execute('CREATE TABLE "stub" (x INT)')
        conn.execute("INSERT INTO stub VALUES (1)")
        conn.execute(
            "INSERT INTO _meta VALUES ('stub', '', 1, 0, current_timestamp, 'local')"
        )
        conn.close()

        def boom():
            raise BQMetadataAuthError("metadata server unreachable: simulated")
        monkeypatch.setattr("src.orchestrator.get_metadata_token", boom)

        with caplog.at_level(logging.ERROR, logger="src.orchestrator"):
            orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
            result = orch.rebuild()

        # Local 'stub' view should still attach — failure of one source shouldn't break others
        assert "bigquery" in result
        assert "stub" in result["bigquery"]
        assert any(
            "metadata" in r.message.lower() and r.levelname == "ERROR"
            for r in caplog.records
        ), f"expected ERROR-level log mentioning metadata; got: {[(r.levelname, r.message) for r in caplog.records]}"
