"""Tests for Keboola extractor."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import duckdb
import pytest


@pytest.fixture
def output_dir(tmp_path):
    d = tmp_path / "extracts" / "keboola"
    d.mkdir(parents=True)
    return str(d)


@pytest.fixture
def sample_configs():
    return [
        {
            "id": "in.c-crm.orders",
            "name": "orders",
            "source_type": "keboola",
            "bucket": "in.c-crm",
            "source_table": "orders",
            "query_mode": "local",
            "description": "Order data",
        },
        {
            "id": "in.c-crm.customers",
            "name": "customers",
            "source_type": "keboola",
            "bucket": "in.c-crm",
            "source_table": "customers",
            "query_mode": "local",
            "description": "Customer data",
        },
    ]


def _mock_attach(conn, url, token):
    """Mock that says extension is available and ATTACHes a fake kbc catalog."""
    # Create in-memory DB as kbc so views referencing kbc."bucket"."table" can be created
    conn.execute("ATTACH ':memory:' AS kbc")
    return True


def _write_parquet(pq_path, data_sql="SELECT 1 AS id, 'test' AS name"):
    """Helper to write a parquet file with given SQL."""
    local_conn = duckdb.connect()
    local_conn.execute(f"COPY ({data_sql}) TO '{pq_path}' (FORMAT PARQUET)")
    local_conn.close()


class TestKeboolaExtractor:
    def test_creates_extract_duckdb(self, output_dir, sample_configs):
        """Test that run() creates extract.duckdb with correct structure."""
        from connectors.keboola.extractor import run

        def write_parquet(conn, tc, pq_path):
            _write_parquet(pq_path)

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach), \
             patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_parquet):
            result = run(output_dir, sample_configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 2
        assert result["tables_failed"] == 0

        # Verify extract.duckdb exists and has correct structure
        db_path = Path(output_dir) / "extract.duckdb"
        assert db_path.exists()

        conn = duckdb.connect(str(db_path))
        try:
            # Check _meta table
            meta = conn.execute("SELECT * FROM _meta ORDER BY table_name").fetchall()
            assert len(meta) == 2
            names = {row[0] for row in meta}
            assert names == {"orders", "customers"}

            # Check all are 'local' query_mode
            modes = {row[5] for row in meta}
            assert modes == {"local"}
        finally:
            conn.close()

    def test_remote_tables_not_downloaded(self, output_dir):
        """Test that tables with query_mode='remote' are registered but not downloaded."""
        from connectors.keboola.extractor import run

        configs = [{
            "name": "big_table",
            "bucket": "in.c-events",
            "source_table": "big_table",
            "query_mode": "remote",
            "description": "Too large to sync",
        }]

        def mock_attach_with_schema(conn, url, token):
            """Mock kbc with the expected bucket schema so remote views can be created."""
            conn.execute("ATTACH ':memory:' AS kbc")
            conn.execute('CREATE SCHEMA kbc."in.c-events"')
            conn.execute('CREATE TABLE kbc."in.c-events"."big_table" (id VARCHAR)')
            return True

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=mock_attach_with_schema):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        try:
            meta = conn.execute("SELECT query_mode FROM _meta WHERE table_name='big_table'").fetchone()
            assert meta[0] == "remote"

            # _remote_attach table should exist with Keboola connection info
            ra = conn.execute("SELECT alias, extension, url, token_env FROM _remote_attach").fetchone()
            assert ra[0] == "kbc"
            assert ra[1] == "keboola"
            assert ra[2] == "https://example.com"
            assert ra[3] == "KEBOOLA_STORAGE_TOKEN"
        finally:
            conn.close()

        # No parquet file should exist
        assert not (Path(output_dir) / "data" / "big_table.parquet").exists()

    def test_handles_extraction_failure(self, output_dir, sample_configs):
        """Test that a failed table doesn't stop other tables from extracting."""
        from connectors.keboola.extractor import run

        call_count = 0
        def side_effect(conn, tc, pq_path):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Network error")
            # Second call succeeds
            _write_parquet(pq_path, "SELECT 1 AS id")

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach), \
             patch("connectors.keboola.extractor._extract_via_extension", side_effect=side_effect):
            result = run(output_dir, sample_configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 1
        assert len(result["errors"]) == 1

    def test_creates_data_directory(self, output_dir, sample_configs):
        """Test that data/ subdirectory is created."""
        from connectors.keboola.extractor import run

        def write_pq(conn, tc, pq_path):
            _write_parquet(pq_path, "SELECT 1 AS id")

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach), \
             patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq):
            run(output_dir, sample_configs, "https://example.com", "test-token")

        assert (Path(output_dir) / "data").is_dir()
        assert (Path(output_dir) / "data" / "orders.parquet").exists()

    def test_views_queryable(self, output_dir):
        """Test that views in extract.duckdb can be queried."""
        from connectors.keboola.extractor import run

        configs = [{"name": "test_table", "query_mode": "local", "description": "Test"}]

        def write_pq(conn, tc, pq_path):
            _write_parquet(pq_path, "SELECT 42 AS value, 'hello' AS msg")

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach), \
             patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq):
            run(output_dir, configs, "https://example.com", "test-token")

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        try:
            result = conn.execute("SELECT value, msg FROM test_table").fetchone()
            assert result[0] == 42
            assert result[1] == "hello"
        finally:
            conn.close()

    def test_meta_table_schema(self, output_dir):
        """Test that _meta table has all required columns."""
        from connectors.keboola.extractor import run

        configs = [{"name": "t", "query_mode": "local", "description": "desc"}]

        def write_pq(conn, tc, pq_path):
            _write_parquet(pq_path, "SELECT 1 AS x")

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach), \
             patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq):
            run(output_dir, configs, "https://example.com", "test-token")

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        try:
            cols = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name='_meta' ORDER BY ordinal_position").fetchall()
            col_names = [c[0] for c in cols]
            assert col_names == ["table_name", "description", "rows", "size_bytes", "extracted_at", "query_mode"]
        finally:
            conn.close()

    def test_legacy_fallback_when_extension_unavailable(self, output_dir):
        """Test that legacy client is used when extension attach fails."""
        from connectors.keboola.extractor import run

        configs = [{"name": "t", "id": "in.c-test.t", "query_mode": "local", "description": ""}]

        def mock_legacy(tc, pq_path, url, token):
            _write_parquet(pq_path, "SELECT 1 AS id")

        # Extension not available
        with patch("connectors.keboola.extractor._try_attach_extension", return_value=False), \
             patch("connectors.keboola.extractor._extract_via_legacy", side_effect=mock_legacy):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 0
