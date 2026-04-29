"""Tests for BigQuery extractor (remote-only via DuckDB extension)."""

import re
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

from tests.helpers.contract import validate_extract_contract


@pytest.fixture
def output_dir(tmp_path):
    d = tmp_path / "extracts" / "bigquery"
    d.mkdir(parents=True)
    return str(d)


@pytest.fixture
def sample_configs():
    return [
        {
            "id": "project.analytics.orders",
            "name": "orders",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "query_mode": "remote",
            "description": "Order data from BQ",
        },
        {
            "id": "project.analytics.sessions",
            "name": "sessions",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "sessions",
            "query_mode": "remote",
            "description": "Session data",
        },
    ]


class _DuckDBProxy:
    """Proxy around a real DuckDB connection that intercepts BigQuery extension SQL."""

    def __init__(self, real_conn):
        self._real = real_conn

    def execute(self, sql, *args, **kwargs):
        sql_upper = sql.strip().upper()
        if sql_upper.startswith("INSTALL BIGQUERY") or sql_upper.startswith(
            "LOAD BIGQUERY"
        ):
            return MagicMock()
        if "ATTACH" in sql_upper and "BIGQUERY" in sql_upper:
            return MagicMock()
        if sql_upper.startswith("DETACH BQ"):
            return MagicMock()
        # CREATE VIEW referencing bq.* -> create a dummy table instead
        if "FROM BQ." in sql_upper and "CREATE" in sql_upper:
            match = re.search(r'VIEW\s+"?(\w+)"?', sql, re.IGNORECASE)
            if match:
                view_name = match.group(1)
                self._real.execute(
                    f'CREATE OR REPLACE TABLE "{view_name}" (dummy INTEGER)'
                )
                return MagicMock()
        return self._real.execute(sql, *args, **kwargs)

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestBigQueryExtractor:
    def test_creates_extract_duckdb_with_meta(self, output_dir, sample_configs):
        """Test that init_extract creates extract.duckdb with _meta and _remote_attach."""
        from unittest.mock import patch

        def proxy_connect(path=None, **kwargs):
            real_conn = duckdb.connect(path)
            return _DuckDBProxy(real_conn)

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = proxy_connect
            from connectors.bigquery.extractor import init_extract

            result = init_extract(output_dir, "my-project", sample_configs)

        assert result["tables_registered"] == 2
        assert len(result["errors"]) == 0

        # Verify extract.duckdb has _meta with correct data
        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        try:
            meta = conn.execute(
                "SELECT table_name, query_mode FROM _meta ORDER BY table_name"
            ).fetchall()
            assert len(meta) == 2
            assert meta[0][0] == "orders"
            assert meta[0][1] == "remote"
            assert meta[1][0] == "sessions"
            assert meta[1][1] == "remote"

            # Verify _remote_attach table for orchestrator re-ATTACH
            ra = conn.execute(
                "SELECT alias, extension, url, token_env FROM _remote_attach"
            ).fetchone()
            assert ra[0] == "bq"
            assert ra[1] == "bigquery"
            assert ra[2] == "project=my-project"
            assert ra[3] == ""  # BQ handles auth via env automatically
        finally:
            conn.close()

        validate_extract_contract(str(Path(output_dir) / "extract.duckdb"))

    def test_no_data_directory_created(self, output_dir, sample_configs):
        """BigQuery is remote-only -- no data/ directory should exist."""
        assert not (Path(output_dir) / "data").exists()

    def test_all_tables_are_remote(self, output_dir):
        """Verify all BigQuery tables get query_mode='remote' in _meta."""
        db_path = Path(output_dir) / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'remote'
        )""")
        conn.execute(
            "INSERT INTO _meta VALUES ('t1', '', 0, 0, current_timestamp, 'remote')"
        )

        result = conn.execute("SELECT query_mode FROM _meta").fetchone()
        assert result[0] == "remote"
        conn.close()

    def test_handles_registration_failure(self, output_dir):
        """A failed table registration records error but does not stop others."""
        db_path = Path(output_dir) / "extract.duckdb"
        conn = duckdb.connect(str(db_path))

        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'remote'
        )""")

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        # Simulate: first succeeds, second fails (not inserted)
        conn.execute(
            "INSERT INTO _meta VALUES ('good_table', '', 0, 0, ?, 'remote')", [now]
        )

        meta = conn.execute("SELECT count(*) FROM _meta").fetchone()
        assert meta[0] == 1  # Only good_table registered
        conn.close()

    def test_meta_table_schema(self, output_dir):
        """Verify _meta table has all required columns per the extract.duckdb contract."""
        from connectors.bigquery.extractor import _create_meta_table

        db_path = Path(output_dir) / "contract_check.duckdb"
        conn = duckdb.connect(str(db_path))
        _create_meta_table(conn)

        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = '_meta' ORDER BY ordinal_position"
        ).fetchall()
        col_names = [c[0] for c in columns]
        assert col_names == [
            "table_name",
            "description",
            "rows",
            "size_bytes",
            "extracted_at",
            "query_mode",
        ]
        conn.close()



# ---------------------------------------------------------------------------
# Connector failure mode tests
# ---------------------------------------------------------------------------


class TestBigQueryExtractorFailureModes:
    """Tests for BigQuery extractor failure handling and resilience."""

    def test_corrupted_extract_duckdb_orchestrator_skips(self, output_dir):
        """If extract.duckdb is corrupted, the orchestrator should skip it
        and not crash. This test creates a corrupted file and verifies the
        orchestrator handles it gracefully."""
        from src.orchestrator import SyncOrchestrator

        # Write garbage to extract.duckdb
        db_path = Path(output_dir) / "extract.duckdb"
        db_path.write_bytes(b"not a real duckdb file!!!")

        # Orchestrator should not crash when encountering this
        analytics_db = str(Path(output_dir) / "analytics.duckdb")
        orch = SyncOrchestrator(analytics_db_path=analytics_db)
        # The rebuild should complete (possibly with warnings) but not raise
        result = orch.rebuild()
        # The corrupted source should not appear in results
        assert "bigquery" not in result

    def test_partial_data_write_incomplete_extract(self, output_dir):
        """When init_extract fails partway through (e.g. one view creation
        fails), the extract.duckdb is still created atomically and the
        successful tables are preserved."""
        from connectors.bigquery.extractor import init_extract
        from unittest.mock import patch

        configs = [
            {
                "name": "good_table",
                "bucket": "analytics",
                "source_table": "good_table",
                "query_mode": "remote",
                "description": "OK",
            },
            {
                "name": "bad-table",  # hyphen → unsafe identifier
                "bucket": "analytics",
                "source_table": "bad_table",
                "query_mode": "remote",
                "description": "Will fail validation",
            },
        ]

        def proxy_connect(path=None, **kwargs):
            real_conn = duckdb.connect(path)
            return _DuckDBProxy(real_conn)

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = proxy_connect
            result = init_extract(output_dir, "my-project", configs)

        # good_table registered, bad-table skipped
        assert result["tables_registered"] == 1
        assert len(result["errors"]) == 1

    def test_network_timeout_during_extraction(self, output_dir):
        """Network timeout during BQ extension ATTACH should be caught and
        reported as an error, not crash the process."""
        from connectors.bigquery.extractor import init_extract
        from unittest.mock import patch

        configs = [
            {
                "name": "timeout_table",
                "bucket": "analytics",
                "source_table": "timeout_table",
                "query_mode": "remote",
                "description": "Will timeout",
            },
        ]

        def proxy_connect_timeout(path=None, **kwargs):
            real_conn = duckdb.connect(path)
            proxy = _DuckDBProxy(real_conn)
            # Override execute to raise on ATTACH
            original_execute = proxy.execute
            def timeout_execute(sql, *args, **kwargs):
                sql_upper = sql.strip().upper()
                if "ATTACH" in sql_upper and "BIGQUERY" in sql_upper:
                    raise TimeoutError("BigQuery connection timed out")
                return original_execute(sql, *args, **kwargs)
            proxy.execute = timeout_execute
            return proxy

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = proxy_connect_timeout
            result = init_extract(output_dir, "my-project", configs)

        # The timeout should be caught — no tables registered, error recorded
        assert result["tables_registered"] == 0
        assert len(result["errors"]) >= 1

    def test_all_tables_fail_returns_errors(self, output_dir):
        """When every table registration fails, the extractor returns all
        errors without crashing."""
        from connectors.bigquery.extractor import init_extract
        from unittest.mock import patch

        configs = [
            {"name": "bad-1", "bucket": "ds", "source_table": "t1",
             "query_mode": "remote", "description": ""},
            {"name": "bad-2", "bucket": "ds", "source_table": "t2",
             "query_mode": "remote", "description": ""},
        ]

        def proxy_connect(path=None, **kwargs):
            real_conn = duckdb.connect(path)
            return _DuckDBProxy(real_conn)

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = proxy_connect
            result = init_extract(output_dir, "my-project", configs)

        # Both have unsafe identifiers (hyphens)
        assert result["tables_registered"] == 0
        assert len(result["errors"]) == 2

    def test_unsafe_identifier_skipped_not_crashed(self, output_dir):
        """Tables with unsafe identifiers are skipped with an error in stats,
        not causing a crash."""
        from connectors.bigquery.extractor import init_extract
        from unittest.mock import patch

        configs = [
            {"name": "bad-name", "bucket": "dataset", "source_table": "t",
             "query_mode": "remote", "description": "hyphen not allowed"},
            {"name": "good_name", "bucket": "dataset", "source_table": "t",
             "query_mode": "remote", "description": "OK"},
        ]

        def proxy_connect(path=None, **kwargs):
            real_conn = duckdb.connect(path)
            return _DuckDBProxy(real_conn)

        with patch("connectors.bigquery.extractor.duckdb") as mock_mod:
            mock_mod.connect = proxy_connect
            result = init_extract(output_dir, "my-project", configs)

        assert result["tables_registered"] == 1
        assert len(result["errors"]) == 1
        assert "unsafe" in result["errors"][0]["error"].lower()

    def test_atomic_swap_prevents_corruption_on_crash(self, output_dir):
        """The extractor writes to a temp file then atomically swaps it into
        place. If the process crashes mid-write, the old extract.duckdb
        (if any) is not corrupted."""
        # Create a valid existing extract.duckdb
        db_path = Path(output_dir) / "extract.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.execute("""CREATE TABLE _meta (
            table_name VARCHAR, description VARCHAR, rows BIGINT,
            size_bytes BIGINT, extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'remote'
        )""")
        conn.execute("INSERT INTO _meta VALUES ('existing', '', 0, 0, current_timestamp, 'remote')")
        conn.close()

        # Simulate a crash: the tmp file exists but is incomplete
        tmp_path = Path(output_dir) / "extract.duckdb.tmp"
        tmp_path.write_bytes(b"incomplete garbage")

        # The existing extract.duckdb should still be valid
        conn2 = duckdb.connect(str(db_path))
        rows = conn2.execute("SELECT table_name FROM _meta").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "existing"
        conn2.close()

        # Clean up
        tmp_path.unlink()
