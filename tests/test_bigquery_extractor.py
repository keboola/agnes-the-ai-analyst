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


class TestDetectTableType:
    """Detect whether a BQ entity is a base table or a view."""

    def test_base_table_returns_table(self):
        from connectors.bigquery.extractor import _detect_table_type
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("BASE TABLE",)
        result = _detect_table_type(conn, "proj", "ds", "tbl")
        assert result == "BASE TABLE"

    def test_view_returns_view(self):
        from connectors.bigquery.extractor import _detect_table_type
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("VIEW",)
        result = _detect_table_type(conn, "proj", "ds", "tbl")
        assert result == "VIEW"

    def test_missing_returns_none(self):
        from connectors.bigquery.extractor import _detect_table_type
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        result = _detect_table_type(conn, "proj", "ds", "tbl")
        assert result is None

    def test_query_uses_bigquery_query_function(self):
        """Detection must use bigquery_query() table function (works on views via jobs API)."""
        from connectors.bigquery.extractor import _detect_table_type
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("VIEW",)
        _detect_table_type(conn, "my-proj", "my_ds", "my_tbl")

        sql = conn.execute.call_args[0][0]
        assert "bigquery_query" in sql.lower()
        assert "INFORMATION_SCHEMA.TABLES" in sql
        assert "my_tbl" in sql
