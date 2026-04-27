"""Tests for DuckDB Manager - query_mode classification and BQ registration.

Tests cover:
- _get_bq_project_from_table_id: extracting BQ project from table IDs
- get_remote_tables: filtering tables by query_mode
- register_bq_table: registering BQ query results in DuckDB
- init_duckdb: table classification by query_mode, local view creation,
  remote table logging
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.duckdb_manager import (
    _get_bq_project_from_table_id,
    get_remote_tables,
    init_duckdb,
    register_bq_table,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal project layout with docs/data_description.md and a parquet file.

    Returns (project_root, db_path, data_dir) tuple.
    """
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    data_description = """\
# Data Description

```yaml
folder_mapping:
  in.c-crm: crm_data

tables:
  - id: "in.c-crm.company"
    name: "company"
    description: "Company master data"
    primary_key: "id"
    sync_strategy: "full_refresh"
```
"""
    (docs_dir / "data_description.md").write_text(data_description)

    # Create parquet directory and a minimal parquet file
    data_dir = tmp_path / "server" / "parquet" / "crm_data"
    data_dir.mkdir(parents=True)

    table = pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"]})
    pq.write_table(table, data_dir / "company.parquet")

    db_dir = tmp_path / "user" / "duckdb"
    db_dir.mkdir(parents=True)
    db_path = str(db_dir / "test.duckdb")

    return tmp_path, db_path, str(tmp_path / "server")


@pytest.fixture
def tmp_project_mixed(tmp_path):
    """Project with local, remote, and hybrid tables."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    data_description = """\
# Data Description

```yaml
folder_mapping:
  in.c-crm: crm_data

tables:
  - id: "in.c-crm.company"
    name: "company"
    description: "Local table"
    primary_key: "id"
    sync_strategy: "full_refresh"

  - id: "prj-example-1234.finance.revenue"
    name: "revenue"
    description: "Remote BQ table"
    primary_key: "id"
    query_mode: "remote"

  - id: "prj-example-1234.marketing.campaigns"
    name: "campaigns"
    description: "Hybrid table"
    primary_key: "id"
    sync_strategy: "full_refresh"
    query_mode: "hybrid"
```
"""
    (docs_dir / "data_description.md").write_text(data_description)

    # Create parquet files for local and hybrid tables
    crm_dir = tmp_path / "server" / "parquet" / "crm_data"
    crm_dir.mkdir(parents=True)
    table = pa.table({"id": [1, 2], "name": ["a", "b"]})
    pq.write_table(table, crm_dir / "company.parquet")

    marketing_dir = tmp_path / "server" / "parquet" / "prj-example-1234.marketing"
    marketing_dir.mkdir(parents=True)
    campaigns_table = pa.table({"id": [10], "campaign": ["test"]})
    pq.write_table(campaigns_table, marketing_dir / "campaigns.parquet")

    db_dir = tmp_path / "user" / "duckdb"
    db_dir.mkdir(parents=True)
    db_path = str(db_dir / "test.duckdb")

    return tmp_path, db_path, str(tmp_path / "server")


@pytest.fixture
def tmp_project_remote_only(tmp_path):
    """Project with only remote tables (no local parquet needed)."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    data_description = """\
# Data Description

```yaml
tables:
  - id: "prj-example-1234.finance.revenue"
    name: "revenue"
    description: "Remote BQ table"
    primary_key: "id"
    query_mode: "remote"

  - id: "prj-example-1234.finance.costs"
    name: "costs"
    description: "Remote BQ table"
    primary_key: "id"
    query_mode: "remote"
```
"""
    (docs_dir / "data_description.md").write_text(data_description)

    db_dir = tmp_path / "user" / "duckdb"
    db_dir.mkdir(parents=True)
    db_path = str(db_dir / "test.duckdb")

    return tmp_path, db_path, str(tmp_path / "server")


# ---------------------------------------------------------------------------
# Tests: _get_bq_project_from_table_id
# ---------------------------------------------------------------------------

class TestGetBqProjectFromTableId:
    """Test extracting BigQuery project ID from fully-qualified table IDs."""

    def test_valid_bq_table_id(self):
        result = _get_bq_project_from_table_id(
            "prj-example-1234.finance.table"
        )
        assert result == "prj-example-1234"

    def test_valid_bq_table_id_different_project(self):
        result = _get_bq_project_from_table_id(
            "my-gcp-project.dataset_name.table_name"
        )
        assert result == "my-gcp-project"

    def test_keboola_format_returns_none(self):
        result = _get_bq_project_from_table_id("in.c-crm.table")
        assert result is None

    def test_two_part_id_returns_none(self):
        result = _get_bq_project_from_table_id("dataset.table")
        assert result is None

    def test_single_part_returns_none(self):
        result = _get_bq_project_from_table_id("table_only")
        assert result is None

    def test_four_parts_returns_none(self):
        result = _get_bq_project_from_table_id("a-b.c.d.e")
        assert result is None

    def test_empty_string_returns_none(self):
        result = _get_bq_project_from_table_id("")
        assert result is None

    def test_three_parts_no_hyphen_returns_none(self):
        result = _get_bq_project_from_table_id("project.dataset.table")
        assert result is None

    def test_hyphen_in_first_part_is_key(self):
        result = _get_bq_project_from_table_id("a-b.dataset.table")
        assert result == "a-b"


# ---------------------------------------------------------------------------
# Tests: get_remote_tables
# ---------------------------------------------------------------------------

class TestGetRemoteTables:
    """Test filtering table configs by query_mode."""

    def test_returns_remote_tables(self):
        configs = [
            {"name": "local", "query_mode": "local"},
            {"name": "remote1", "query_mode": "remote"},
            {"name": "hybrid1", "query_mode": "hybrid"},
        ]
        result = get_remote_tables(configs)
        names = [tc["name"] for tc in result]
        assert "remote1" in names
        assert "hybrid1" in names
        assert "local" not in names

    def test_returns_empty_when_all_local(self):
        configs = [
            {"name": "t1", "query_mode": "local"},
            {"name": "t2"},  # defaults to local (no query_mode key)
        ]
        result = get_remote_tables(configs)
        assert result == []

    def test_missing_query_mode_treated_as_local(self):
        configs = [{"name": "t1"}]  # no query_mode
        result = get_remote_tables(configs)
        assert result == []


# ---------------------------------------------------------------------------
# Tests: register_bq_table
# ---------------------------------------------------------------------------

class TestRegisterBqTable:
    """Test registering BQ query results as DuckDB views."""

    @staticmethod
    def _make_factory(arrow_table, side_effect=None):
        """Create a mock BQ client factory returning a client that yields arrow_table."""
        mock_job = MagicMock()
        if side_effect:
            mock_job.to_arrow.side_effect = side_effect
        else:
            mock_job.to_arrow.return_value = arrow_table
        mock_client = MagicMock()
        mock_client.query.return_value = mock_job
        factory = MagicMock(return_value=mock_client)
        factory._mock_client = mock_client
        factory._mock_job = mock_job
        return factory

    def test_registers_arrow_table_in_duckdb(self):
        """Result from BQ should be queryable in DuckDB after registration."""
        arrow_table = pa.table({"id": [1, 2], "val": [10.0, 20.0]})
        factory = self._make_factory(arrow_table)

        conn = duckdb.connect()
        rows = register_bq_table(
            conn=conn,
            table_id="proj.dataset.table",
            view_name="test_view",
            sql="SELECT * FROM table",
            bq_project="test-project",
            _bq_client_factory=factory,
        )

        assert rows == 2
        result = conn.execute("SELECT SUM(val) FROM test_view").fetchone()[0]
        assert result == 30.0
        conn.close()

    def test_raises_without_bq_project(self):
        conn = duckdb.connect()
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="BigQuery project not set"):
                register_bq_table(
                    conn=conn,
                    table_id="proj.ds.tbl",
                    view_name="v",
                    sql="SELECT 1",
                )
        conn.close()

    def test_uses_env_var_when_no_project_arg(self):
        arrow_table = pa.table({"x": [1]})
        factory = self._make_factory(arrow_table)

        conn = duckdb.connect()
        with patch.dict(os.environ, {"BIGQUERY_PROJECT": "env-proj"}):
            register_bq_table(
                conn=conn,
                table_id="p.d.t",
                view_name="v",
                sql="SELECT 1",
                _bq_client_factory=factory,
            )

        factory.assert_called_once_with("env-proj")
        conn.close()

    def test_storage_api_fallback(self):
        """Falls back to REST when Storage API permission denied."""
        arrow_table = pa.table({"x": [1]})
        factory = self._make_factory(
            arrow_table,
            side_effect=[
                Exception("PERMISSION_DENIED readsessions"),
                arrow_table,
            ],
        )

        conn = duckdb.connect()
        rows = register_bq_table(
            conn=conn,
            table_id="p.d.t",
            view_name="v",
            sql="SELECT 1",
            bq_project="proj",
            _bq_client_factory=factory,
        )

        assert rows == 1
        factory._mock_job.to_arrow.assert_called_with(create_bqstorage_client=False)
        conn.close()


# ---------------------------------------------------------------------------
# Tests: init_duckdb - table classification
# ---------------------------------------------------------------------------

class TestInitDuckdbClassification:
    """Test that tables are correctly classified by query_mode."""

    def test_local_tables_create_parquet_views(self, tmp_project):
        project_root, db_path, data_dir = tmp_project

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root):
            result = init_duckdb(
                db_path=db_path, data_dir=data_dir, verbose=False
            )

        assert result is True

        conn = duckdb.connect(db_path, read_only=True)
        tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
        assert "company" in tables
        row_count = conn.execute("SELECT COUNT(*) FROM company").fetchone()[0]
        assert row_count == 3
        conn.close()

    def test_remote_tables_not_created_as_local_views(self, tmp_project_mixed):
        project_root, db_path, data_dir = tmp_project_mixed

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root):
            result = init_duckdb(
                db_path=db_path, data_dir=data_dir, verbose=False
            )

        assert result is True

        conn = duckdb.connect(db_path, read_only=True)
        tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
        assert "revenue" not in tables
        assert "company" in tables
        conn.close()

    def test_hybrid_tables_create_local_views(self, tmp_project_mixed):
        project_root, db_path, data_dir = tmp_project_mixed

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root):
            result = init_duckdb(
                db_path=db_path, data_dir=data_dir, verbose=False
            )

        assert result is True

        conn = duckdb.connect(db_path, read_only=True)
        tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
        assert "campaigns" in tables
        conn.close()

    def test_default_query_mode_is_local(self, tmp_project):
        project_root, db_path, data_dir = tmp_project

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root):
            result = init_duckdb(
                db_path=db_path, data_dir=data_dir, verbose=False
            )

        assert result is True

        conn = duckdb.connect(db_path, read_only=True)
        tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
        assert "company" in tables
        conn.close()


# ---------------------------------------------------------------------------
# Tests: init_duckdb - remote table logging
# ---------------------------------------------------------------------------

class TestInitDuckdbRemoteLogging:
    """Test that remote tables are logged correctly."""

    def test_remote_tables_logged(self, tmp_project_remote_only, capsys):
        project_root, db_path, data_dir = tmp_project_remote_only

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root):
            init_duckdb(
                db_path=db_path, data_dir=data_dir, verbose=True,
            )

        output = capsys.readouterr().out
        assert "revenue" in output
        assert "costs" in output
        assert "[BQ]" in output

    def test_remote_only_project_succeeds(self, tmp_project_remote_only):
        project_root, db_path, data_dir = tmp_project_remote_only

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root):
            result = init_duckdb(
                db_path=db_path, data_dir=data_dir, verbose=False,
            )

        assert result is True


# ---------------------------------------------------------------------------
# Tests: init_duckdb - missing parquet handling
# ---------------------------------------------------------------------------

class TestInitDuckdbMissingParquet:
    """Test behavior when parquet files are missing."""

    def test_missing_parquet_skips_view(self, tmp_path):
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()

        data_description = """\
# Data Description

```yaml
tables:
  - id: "in.c-crm.missing_table"
    name: "missing_table"
    description: "No parquet exists"
    primary_key: "id"
    sync_strategy: "full_refresh"
```
"""
        (docs_dir / "data_description.md").write_text(data_description)

        db_dir = tmp_path / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        db_path = str(db_dir / "test.duckdb")

        with patch("scripts.duckdb_manager.find_project_root", return_value=tmp_path):
            result = init_duckdb(
                db_path=db_path, data_dir=str(tmp_path / "server"), verbose=False
            )

        assert result is True

        conn = duckdb.connect(db_path, read_only=True)
        tables = [row[0] for row in conn.execute("SHOW TABLES").fetchall()]
        assert "missing_table" not in tables
        conn.close()

    def test_remote_table_no_local_parquet_needed(self, tmp_project_remote_only):
        project_root, db_path, data_dir = tmp_project_remote_only

        with patch("scripts.duckdb_manager.find_project_root", return_value=project_root):
            result = init_duckdb(
                db_path=db_path, data_dir=data_dir, verbose=False,
            )

        assert result is True
