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
        conn.execute("INSERT INTO _meta VALUES ('orders', '', 1, 0, current_timestamp, 'local')")
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
        conn.execute("INSERT INTO _meta VALUES ('orders', '', 0, 0, current_timestamp, 'local')")
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
        conn.execute("INSERT INTO _meta VALUES ('orders', '', 0, 0, current_timestamp, 'local')")
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
            setup_env["extracts_dir"],
            "keboola",
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
        conn.execute("INSERT INTO _meta VALUES ('orders', '', 1, 0, current_timestamp, 'local')")

        # Malicious table_name in _meta (no actual table needed — validation rejects before access)
        conn.execute("INSERT INTO _meta VALUES ('evil; DROP TABLE users--', '', 0, 0, current_timestamp, 'local')")
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
        conn.execute("INSERT INTO _remote_attach VALUES ('bq', 'bigquery', 'project=test-proj', '')")
        # Local stub view so rebuild has something to attach (avoids INSTALL bigquery in test)
        conn.execute('CREATE TABLE "stub" (x INT)')
        conn.execute("INSERT INTO stub VALUES (1)")
        conn.execute("INSERT INTO _meta VALUES ('stub', '', 1, 0, current_timestamp, 'local')")
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
        assert any("CREATE OR REPLACE SECRET" in s.upper() and "TYPE BIGQUERY" in s.upper() for s in captured), (
            "orchestrator must create DuckDB secret with metadata token"
        )
        # ATTACH for BQ must not include TOKEN= clause (auth is via the secret)
        attach_for_bq = [s for s in captured if s.upper().startswith("ATTACH ") and "TYPE BIGQUERY" in s.upper()]
        assert attach_for_bq, "expected ATTACH for the bq alias"
        assert all("TOKEN '" not in s for s in attach_for_bq), (
            f"ATTACH for BQ must not pass TOKEN= directly (auth via secret); got: {attach_for_bq}"
        )

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
        conn.execute("INSERT INTO _remote_attach VALUES ('bq', 'bigquery', 'project=test-proj', '')")
        conn.execute('CREATE TABLE "stub" (x INT)')
        conn.execute("INSERT INTO stub VALUES (1)")
        conn.execute("INSERT INTO _meta VALUES ('stub', '', 1, 0, current_timestamp, 'local')")
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
        assert any("metadata" in r.message.lower() and r.levelname == "ERROR" for r in caplog.records), (
            f"expected ERROR-level log mentioning metadata; got: {[(r.levelname, r.message) for r in caplog.records]}"
        )


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

        # Empty source is omitted from result (no valid tables to expose)
        assert "empty_source" not in result

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

        # Source is omitted from result (all table names failed validation)
        assert "bad_names" not in result

    def test_mid_write_extract_duckdb_handled_gracefully(self, setup_env):
        """If an extractor is mid-write (tmp file exists but hasn't been
        swapped yet), the orchestrator should not crash."""
        from src.orchestrator import SyncOrchestrator

        source_dir = setup_env["extracts_dir"] / "midwrite"
        source_dir.mkdir()
        # Only a .tmp file — no extract.duckdb yet
        tmp = source_dir / "extract.duckdb.tmp"
        tmp.write_bytes(b"partial write in progress")

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        # midwrite source is omitted (no extract.duckdb)
        assert "midwrite" not in result

    def test_multiple_corrupted_sources_do_not_block_others(self, setup_env):
        """Multiple corrupted sources should not prevent valid ones from being processed."""
        from src.orchestrator import SyncOrchestrator

        # Create two corrupted sources
        for name in ["corrupt_a", "corrupt_b"]:
            d = setup_env["extracts_dir"] / name
            d.mkdir()
            (d / "extract.duckdb").write_bytes(b"garbage " + name.encode())

        # And a valid one
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


# ----------------------------------------------------------------------------
# 0.41.0 — filesystem-fallback master views for materialized parquets that
# couldn't register themselves in extract.duckdb's _meta (the open-as-second-
# write-handle race that 0.40.0's _persist_materialized_inner_view hits).
# ----------------------------------------------------------------------------


def _write_minimal_parquet(path: Path, n_rows: int = 3) -> None:
    """Write a tiny valid parquet file using DuckDB's COPY. Used to seed
    the filesystem-fallback test cases where we want a parquet on disk
    that the extractor never registered in _meta."""
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(f"CREATE TABLE t AS SELECT range AS id FROM range({n_rows})")
        safe = str(path).replace("'", "''")
        conn.execute(f"COPY (SELECT * FROM t) TO '{safe}' (FORMAT PARQUET)")
    finally:
        conn.close()


def _seed_registry_materialized_row(table_id: str, source_type: str = "bigquery") -> None:
    """Insert a `query_mode='materialized'` row into table_registry so
    the filesystem-fallback scan recognises the parquet as live (not
    orphan from a deleted registry row).

    Uses the same `get_system_db` + `TableRegistryRepository` path the
    orchestrator's fallback uses internally."""
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=table_id,
            name=table_id,
            source_type=source_type,
            bucket="bkt",
            source_table=table_id,
            source_query="SELECT 1 AS id",
            query_mode="materialized",
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


class TestFilesystemFallbackMasterViews:
    """If a parquet exists on disk but never made it into _meta (the
    open-handle race that bit 0.40.0), the orchestrator must still
    create a master view over it via read_parquet()."""

    def test_filesystem_fallback_creates_master_view(self, setup_env):
        from src.orchestrator import SyncOrchestrator

        # Set up an extract.duckdb with ONLY remote rows in _meta.
        _create_mock_extract(
            setup_env["extracts_dir"],
            "bigquery",
            tables=[
                {"name": "remote_one", "data": [{"x": "1"}], "query_mode": "remote"},
            ],
        )

        # Drop a parquet on disk for a table that's NOT in _meta — this
        # is the materialize_query "atomic swap succeeded but _meta
        # registration hit lock conflict" scenario.
        data_dir = setup_env["extracts_dir"] / "bigquery" / "data"
        _write_minimal_parquet(data_dir / "order_economics.parquet", n_rows=42)
        # Seed the matching materialized registry row — orphan parquets
        # without a registry row are deliberately skipped.
        _seed_registry_materialized_row("order_economics")

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        # Both views should appear: the meta-path one and the
        # filesystem-fallback one.
        assert "bigquery" in result
        assert "remote_one" in result["bigquery"]
        assert "order_economics" in result["bigquery"], (
            "filesystem-fallback master view must be created when parquet exists on disk but _meta row is missing"
        )

        # Verify the master view actually queries the parquet.
        master = duckdb.connect(setup_env["analytics_db"], read_only=True)
        try:
            n = master.execute("SELECT COUNT(*) FROM order_economics").fetchone()[0]
            assert n == 42, f"master view should return parquet rows, got {n}"
        finally:
            master.close()

    def test_filesystem_fallback_overwrites_stale_sync_state_error(self, setup_env):
        """A table published via the fallback path must record success in
        sync_state — previously the fallback created the view but left any
        stale set_error() row in place, so the admin UI kept reporting a
        long-fixed failure (seen live: a May-era error row shadowing a
        healthy table for two months)."""
        from src.orchestrator import SyncOrchestrator
        from src.repositories import sync_state_repo

        _create_mock_extract(
            setup_env["extracts_dir"],
            "bigquery",
            tables=[
                {"name": "remote_one", "data": [{"x": "1"}], "query_mode": "remote"},
            ],
        )
        data_dir = setup_env["extracts_dir"] / "bigquery" / "data"
        _write_minimal_parquet(data_dir / "order_economics.parquet", n_rows=7)
        _seed_registry_materialized_row("order_economics")

        # Stale failure from a prior run — exactly what the fallback is
        # recovering from.
        sync_state_repo().set_error("order_economics", "No connection adapters were found for 'gs://…'")

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()
        assert "order_economics" in result["bigquery"]

        state = sync_state_repo().get_table_state("order_economics")
        assert state is not None
        assert state.get("status") == "ok", (
            f"fallback publish must clear the stale error row, got {state.get('status')!r}: {state.get('error')!r}"
        )
        assert int(state.get("rows") or 0) == 7
        assert len(state.get("hash") or "") == 32  # full content MD5

    def test_filesystem_fallback_does_not_duplicate_meta_path(self, setup_env, caplog):
        """When the same name is in BOTH _meta (with an inner view) AND
        on disk as a parquet, the meta path wins — filesystem fallback
        must not create a second / replacement view that shadows the
        meta-driven one."""
        from src.orchestrator import SyncOrchestrator

        _create_mock_extract(
            setup_env["extracts_dir"],
            "bigquery",
            tables=[
                {
                    "name": "order_economics",
                    "data": [{"x": "from_meta"}],
                    "query_mode": "materialized",
                },
            ],
        )
        # Also drop a parquet of the same name on disk.
        data_dir = setup_env["extracts_dir"] / "bigquery" / "data"
        _write_minimal_parquet(data_dir / "order_economics.parquet", n_rows=99)
        _seed_registry_materialized_row("order_economics")

        import logging

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        with caplog.at_level(logging.INFO, logger="src.orchestrator"):
            result = orch.rebuild()

        # `order_economics` should appear exactly once in the result list —
        # meta path won, fallback skipped because the name was already
        # in the per-source `tables` set.
        bq_tables = result.get("bigquery", [])
        assert bq_tables.count("order_economics") == 1, f"name should appear exactly once, got {bq_tables}"
        # Fallback log line must NOT have fired for this name.
        fallback_lines = [
            r.getMessage()
            for r in caplog.records
            if "filesystem-fallback master view created" in r.getMessage() and "order_economics" in r.getMessage()
        ]
        assert fallback_lines == [], (
            f"filesystem-fallback must not fire when meta path is viable; got: {fallback_lines}"
        )

    def test_filesystem_fallback_skips_invalid_table_id(self, setup_env, tmp_path):
        """A parquet whose stem doesn't pass identifier validation
        (e.g. starts with a digit or contains spaces) must be skipped,
        not crash the rebuild."""
        from src.orchestrator import SyncOrchestrator

        _create_mock_extract(
            setup_env["extracts_dir"],
            "bigquery",
            tables=[
                {"name": "remote_one", "data": [{"x": "1"}], "query_mode": "remote"},
            ],
        )

        # Identifier validator rejects names starting with a digit.
        data_dir = setup_env["extracts_dir"] / "bigquery" / "data"
        _write_minimal_parquet(data_dir / "9bad_name.parquet")
        # Even though the registry would let it through, the identifier
        # validator should still reject. (Seed registry to isolate the
        # validator path from the orphan-skip path.)
        try:
            _seed_registry_materialized_row("9bad_name")
        except Exception:
            # Registry row insert may itself reject the bad id; that's
            # fine — the orchestrator scan never sees it then either.
            pass

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()

        # remote_one still works; bad-named parquet is silently skipped.
        assert "remote_one" in result["bigquery"]
        assert "9bad_name" not in result["bigquery"]
        # Master DB has no such view.
        master = duckdb.connect(setup_env["analytics_db"], read_only=True)
        try:
            tables = [
                r[0]
                for r in master.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()
            ]
            assert "9bad_name" not in tables
        finally:
            master.close()

    def test_filesystem_fallback_skips_orphan_parquet(self, setup_env):
        """Orphan parquets — files left on disk after `DELETE
        /api/admin/registry/{id}` either crashed mid-cleanup or the
        operator dropped the row but the file lingers — must NOT get a
        master view. Otherwise the deleted table would resurrect on next
        rebuild, defeating the unregister contract.

        Existing test `test_orchestrator_skips_orphan_parquet_in_extracts`
        in `tests/test_admin_unregister_cleanup.py` pins this rule for the
        wider unregister flow; this test pins it specifically for the
        new filesystem-fallback path."""
        from src.orchestrator import SyncOrchestrator

        _create_mock_extract(
            setup_env["extracts_dir"],
            "bigquery",
            tables=[
                {"name": "remote_one", "data": [{"x": "1"}], "query_mode": "remote"},
            ],
        )
        # Orphan parquet — NO registry row.
        data_dir = setup_env["extracts_dir"] / "bigquery" / "data"
        _write_minimal_parquet(data_dir / "deleted_table.parquet")
        # NOT calling _seed_registry_materialized_row here — that's the
        # whole point: registry row is missing.

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        result = orch.rebuild()
        bq_tables = result.get("bigquery", [])
        assert "deleted_table" not in bq_tables, f"orphan parquet must NOT resurrect as a master view; got {bq_tables}"

    def test_filesystem_fallback_no_data_dir_is_safe(self, setup_env):
        """Sources without a `<extract_dir>/data/` directory (e.g. the
        BigQuery extractor in remote-only mode pre-#160) must not crash
        the fallback scan."""
        from src.orchestrator import SyncOrchestrator

        # Create a source with extract.duckdb but no `data/` subdir.
        source_dir = setup_env["extracts_dir"] / "bigquery"
        source_dir.mkdir()
        db_path = source_dir / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute(
            "CREATE TABLE _meta (table_name VARCHAR, description VARCHAR, "
            "rows BIGINT, size_bytes BIGINT, extracted_at TIMESTAMP, "
            "query_mode VARCHAR)"
        )
        conn.close()

        orch = SyncOrchestrator(analytics_db_path=setup_env["analytics_db"])
        # Must not crash.
        orch.rebuild()
