"""Tests for the BigQuery client connector.

All external dependencies (google.cloud.bigquery, src.config) are mocked.
Tests cover initialization, metadata caching, schema building, query methods,
and connection testing.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pyarrow as pa
import pytest

# Pre-populate sys.modules with a mock google.cloud.bigquery if not installed,
# so the client module can be imported without the real SDK.
_bq_mock_installed = False
try:
    from google.cloud import bigquery as _bq_test  # noqa: F401
except ImportError:
    _bq_mock_installed = True
    _mock_bigquery = MagicMock()
    # Expose commonly used classes as MagicMock so the client module
    # can reference bigquery.Client, bigquery.QueryJobConfig, etc.
    sys.modules.setdefault("google", MagicMock())
    sys.modules.setdefault("google.cloud", MagicMock())
    sys.modules.setdefault("google.cloud.bigquery", _mock_bigquery)

from connectors.bigquery.client import (
    BIGQUERY_TO_PYARROW_TYPES,
    BigQueryClient,
    create_client,
)

# Import the real or mock bigquery reference used in the client module
from google.cloud import bigquery


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bq_field(name: str, field_type: str, description: str = None):
    """Create a mock BigQuery SchemaField."""
    field = MagicMock()
    field.name = name
    field.field_type = field_type
    field.description = description
    return field


def _make_table_ref(
    table_id: str = "my-project.my_dataset.my_table",
    schema=None,
    num_rows: int = 1000,
    num_bytes: int = 50000,
    created: datetime = None,
    modified: datetime = None,
    time_partitioning=None,
):
    """Create a mock BigQuery Table reference object."""
    table_ref = MagicMock()
    table_ref.table_id = table_id.split(".")[-1]
    table_ref.dataset_id = table_id.split(".")[1] if "." in table_id else "dataset"
    table_ref.project = table_id.split(".")[0] if "." in table_id else "project"
    table_ref.schema = schema or []
    table_ref.num_rows = num_rows
    table_ref.num_bytes = num_bytes
    table_ref.created = created or datetime(2025, 1, 1, 12, 0, 0)
    table_ref.modified = modified or datetime(2025, 6, 1, 12, 0, 0)
    table_ref.time_partitioning = time_partitioning
    return table_ref


@pytest.fixture
def mock_config(tmp_path):
    """Mock get_config() to return a config with metadata path in tmp_path."""
    config = MagicMock()
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    config.get_metadata_path.return_value = metadata_dir
    return config


@pytest.fixture
def mock_bq_client():
    """Create a mock BigQuery Client."""
    return MagicMock()


@pytest.fixture
def client(mock_config, mock_bq_client):
    """Create a BigQueryClient instance with mocked dependencies."""
    with (
        patch("connectors.bigquery.client.bigquery.Client", return_value=mock_bq_client),
        patch("connectors.bigquery.client.get_config", return_value=mock_config),
        patch.dict("os.environ", {"BIGQUERY_PROJECT": "test-project"}),
    ):
        bq_client = BigQueryClient()
    return bq_client


# ---------------------------------------------------------------------------
# 1. Init validates BIGQUERY_PROJECT env var
# ---------------------------------------------------------------------------

class TestInit:
    def test_raises_value_error_when_project_not_set(self, mock_config):
        """Init raises ValueError if project_id is None and env var is missing."""
        with (
            patch("connectors.bigquery.client.bigquery.Client"),
            patch("connectors.bigquery.client.get_config", return_value=mock_config),
            patch.dict("os.environ", {}, clear=True),
        ):
            with pytest.raises(ValueError, match="BigQuery project ID not set"):
                BigQueryClient()

    def test_raises_value_error_when_project_empty_string(self, mock_config):
        """Init raises ValueError if BIGQUERY_PROJECT is set to empty string."""
        with (
            patch("connectors.bigquery.client.bigquery.Client"),
            patch("connectors.bigquery.client.get_config", return_value=mock_config),
            patch.dict("os.environ", {"BIGQUERY_PROJECT": ""}, clear=True),
        ):
            with pytest.raises(ValueError, match="BigQuery project ID not set"):
                BigQueryClient()

    # -------------------------------------------------------------------
    # 2. Init creates client with correct project_id
    # -------------------------------------------------------------------

    def test_creates_client_with_env_project_id(self, mock_config):
        """Client uses BIGQUERY_PROJECT from environment."""
        mock_bq = MagicMock()
        with (
            patch("connectors.bigquery.client.bigquery.Client", return_value=mock_bq) as bq_cls,
            patch("connectors.bigquery.client.get_config", return_value=mock_config),
            patch.dict("os.environ", {"BIGQUERY_PROJECT": "env-project-123"}),
        ):
            client = BigQueryClient()
            bq_cls.assert_called_once_with(project="env-project-123")
            assert client.project_id == "env-project-123"

    def test_creates_client_with_explicit_project_id(self, mock_config):
        """Explicit project_id argument takes precedence over env var."""
        mock_bq = MagicMock()
        with (
            patch("connectors.bigquery.client.bigquery.Client", return_value=mock_bq) as bq_cls,
            patch("connectors.bigquery.client.get_config", return_value=mock_config),
        ):
            client = BigQueryClient(project_id="explicit-project")
            bq_cls.assert_called_once_with(project="explicit-project")
            assert client.project_id == "explicit-project"


# ---------------------------------------------------------------------------
# 3. get_table_metadata fetches and caches metadata correctly
# ---------------------------------------------------------------------------

class TestGetTableMetadata:
    def test_fetches_metadata_from_bigquery(self, client, mock_bq_client):
        """get_table_metadata calls client.get_table and returns correct dict."""
        table_id = "proj.dataset.orders"
        schema = [
            _make_bq_field("order_id", "INTEGER"),
            _make_bq_field("customer_name", "STRING", description="Full name"),
            _make_bq_field("created_at", "TIMESTAMP"),
        ]
        table_ref = _make_table_ref(
            table_id=table_id,
            schema=schema,
            num_rows=5000,
            num_bytes=120000,
        )
        mock_bq_client.get_table.return_value = table_ref

        metadata = client.get_table_metadata(table_id, use_cache=False)

        mock_bq_client.get_table.assert_called_once_with(table_id)
        assert metadata["table_id"] == table_id
        assert metadata["name"] == "orders"
        assert metadata["dataset"] == "dataset"
        assert metadata["project"] == "proj"
        assert metadata["columns"] == ["order_id", "customer_name", "created_at"]
        assert metadata["column_types"]["order_id"] == "INTEGER"
        assert metadata["column_types"]["customer_name"] == "STRING"
        assert metadata["column_types"]["created_at"] == "TIMESTAMP"
        assert metadata["column_descriptions"]["customer_name"] == "Full name"
        assert "order_id" not in metadata["column_descriptions"]
        assert metadata["row_count"] == 5000
        assert metadata["size_bytes"] == 120000
        assert "_cached_at" in metadata

    def test_caches_metadata_in_memory(self, client, mock_bq_client):
        """After first fetch, metadata is stored in the in-memory cache."""
        table_id = "proj.dataset.tbl"
        table_ref = _make_table_ref(table_id=table_id)
        mock_bq_client.get_table.return_value = table_ref

        client.get_table_metadata(table_id, use_cache=False)

        assert table_id in client.metadata_cache
        assert client.metadata_cache[table_id]["table_id"] == table_id

    def test_captures_partitioning_info(self, client, mock_bq_client):
        """Partitioning metadata is captured when table is partitioned."""
        table_id = "proj.dataset.events"
        partition = MagicMock()
        partition.type_ = "DAY"
        partition.field = "event_date"
        partition.expiration_ms = 7776000000

        table_ref = _make_table_ref(table_id=table_id, time_partitioning=partition)
        mock_bq_client.get_table.return_value = table_ref

        metadata = client.get_table_metadata(table_id, use_cache=False)

        assert metadata["partitioning"] is not None
        assert metadata["partitioning"]["type"] == "DAY"
        assert metadata["partitioning"]["field"] == "event_date"
        assert metadata["partitioning"]["expiration_ms"] == 7776000000

    def test_no_partitioning_when_absent(self, client, mock_bq_client):
        """Partitioning is None when table has no partitioning."""
        table_id = "proj.dataset.simple"
        table_ref = _make_table_ref(table_id=table_id, time_partitioning=None)
        mock_bq_client.get_table.return_value = table_ref

        metadata = client.get_table_metadata(table_id, use_cache=False)
        assert metadata["partitioning"] is None

    # -------------------------------------------------------------------
    # 4. get_table_metadata uses cache when available (within TTL)
    # -------------------------------------------------------------------

    def test_uses_cache_within_ttl(self, client, mock_bq_client):
        """When cache is fresh (within TTL), BQ API is not called again."""
        table_id = "proj.dataset.cached_tbl"
        now = datetime.now()
        client.metadata_cache[table_id] = {
            "table_id": table_id,
            "columns": ["a", "b"],
            "column_types": {"a": "STRING", "b": "INTEGER"},
            "_cached_at": now.isoformat(),
        }

        result = client.get_table_metadata(table_id, use_cache=True, cache_ttl_hours=24)

        mock_bq_client.get_table.assert_not_called()
        assert result["table_id"] == table_id
        assert result["columns"] == ["a", "b"]

    def test_refetches_when_cache_expired(self, client, mock_bq_client):
        """When cache is older than TTL, metadata is re-fetched from BQ."""
        table_id = "proj.dataset.stale_tbl"
        old_time = (datetime.now() - timedelta(hours=48)).isoformat()
        client.metadata_cache[table_id] = {
            "table_id": table_id,
            "columns": ["old_col"],
            "column_types": {"old_col": "STRING"},
            "_cached_at": old_time,
        }

        table_ref = _make_table_ref(
            table_id=table_id,
            schema=[_make_bq_field("new_col", "INTEGER")],
        )
        mock_bq_client.get_table.return_value = table_ref

        result = client.get_table_metadata(table_id, use_cache=True, cache_ttl_hours=24)

        mock_bq_client.get_table.assert_called_once_with(table_id)
        assert result["columns"] == ["new_col"]

    def test_bypasses_cache_when_use_cache_false(self, client, mock_bq_client):
        """When use_cache=False, always fetches from BQ even if cache is fresh."""
        table_id = "proj.dataset.force_fetch"
        client.metadata_cache[table_id] = {
            "table_id": table_id,
            "columns": ["cached"],
            "column_types": {"cached": "STRING"},
            "_cached_at": datetime.now().isoformat(),
        }

        table_ref = _make_table_ref(
            table_id=table_id,
            schema=[_make_bq_field("fresh", "INTEGER")],
        )
        mock_bq_client.get_table.return_value = table_ref

        result = client.get_table_metadata(table_id, use_cache=False)
        mock_bq_client.get_table.assert_called_once()
        assert result["columns"] == ["fresh"]


# ---------------------------------------------------------------------------
# 5. get_pyarrow_schema builds correct schema from BQ types
# ---------------------------------------------------------------------------

class TestGetPyarrowSchema:
    def test_builds_correct_schema(self, client, mock_bq_client):
        """Schema maps BQ types to correct PyArrow types."""
        table_id = "proj.dataset.typed_tbl"
        schema = [
            _make_bq_field("id", "INT64"),
            _make_bq_field("name", "STRING"),
            _make_bq_field("price", "FLOAT64"),
            _make_bq_field("active", "BOOLEAN"),
            _make_bq_field("created", "DATE"),
            _make_bq_field("updated_at", "TIMESTAMP"),
        ]
        table_ref = _make_table_ref(table_id=table_id, schema=schema)
        mock_bq_client.get_table.return_value = table_ref

        pa_schema = client.get_pyarrow_schema(table_id)

        assert pa_schema is not None
        assert pa_schema.field("id").type == pa.int64()
        assert pa_schema.field("name").type == pa.string()
        assert pa_schema.field("price").type == pa.float64()
        assert pa_schema.field("active").type == pa.bool_()
        assert pa_schema.field("created").type == pa.date32()
        assert pa_schema.field("updated_at").type == pa.timestamp("us", tz="UTC")

    def test_returns_none_when_no_column_types(self, client):
        """Returns None when metadata has no column_types."""
        table_id = "proj.dataset.empty_schema"
        client.metadata_cache[table_id] = {
            "table_id": table_id,
            "columns": [],
            "column_types": {},
            "_cached_at": datetime.now().isoformat(),
        }

        result = client.get_pyarrow_schema(table_id)
        assert result is None

    def test_unknown_type_falls_back_to_string(self, client, mock_bq_client):
        """Unknown BQ types default to pa.string() in the schema."""
        table_id = "proj.dataset.exotic_types"
        schema = [_make_bq_field("exotic_col", "SOME_UNKNOWN_TYPE")]
        table_ref = _make_table_ref(table_id=table_id, schema=schema)
        mock_bq_client.get_table.return_value = table_ref

        pa_schema = client.get_pyarrow_schema(table_id)
        assert pa_schema.field("exotic_col").type == pa.string()


# ---------------------------------------------------------------------------
# 6. get_date_columns returns only DATE columns
# ---------------------------------------------------------------------------

class TestGetDateColumns:
    def test_returns_only_date_columns(self, client, mock_bq_client):
        """Only columns with BQ type DATE are returned."""
        table_id = "proj.dataset.mixed_dates"
        schema = [
            _make_bq_field("event_date", "DATE"),
            _make_bq_field("created_at", "TIMESTAMP"),
            _make_bq_field("name", "STRING"),
            _make_bq_field("birth_date", "DATE"),
            _make_bq_field("updated_ts", "DATETIME"),
        ]
        table_ref = _make_table_ref(table_id=table_id, schema=schema)
        mock_bq_client.get_table.return_value = table_ref

        date_cols = client.get_date_columns(table_id)
        assert sorted(date_cols) == ["birth_date", "event_date"]

    def test_returns_empty_when_no_date_columns(self, client, mock_bq_client):
        """Returns empty list when no DATE columns exist."""
        table_id = "proj.dataset.no_dates"
        schema = [
            _make_bq_field("id", "INTEGER"),
            _make_bq_field("ts", "TIMESTAMP"),
        ]
        table_ref = _make_table_ref(table_id=table_id, schema=schema)
        mock_bq_client.get_table.return_value = table_ref

        date_cols = client.get_date_columns(table_id)
        assert date_cols == []


# ---------------------------------------------------------------------------
# 7. query_to_arrow executes SQL and returns PyArrow table
# ---------------------------------------------------------------------------

class TestQueryToArrow:
    def test_executes_query_and_returns_arrow(self, client, mock_bq_client):
        """query_to_arrow passes SQL to BQ and returns the arrow result."""
        expected_table = pa.table({"col1": [1, 2, 3]})
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = expected_table
        mock_bq_client.query.return_value = mock_job

        with patch("connectors.bigquery.client.bigquery") as mock_bq_module:
            mock_bq_module.QueryJobConfig.return_value = MagicMock(query_parameters=None)
            client.client = mock_bq_client

            result = client.query_to_arrow("SELECT * FROM `proj.dataset.tbl`")

        mock_bq_client.query.assert_called_once()
        call_args = mock_bq_client.query.call_args
        assert call_args[0][0] == "SELECT * FROM `proj.dataset.tbl`"
        assert result.equals(expected_table)

    def test_passes_query_parameters(self, client, mock_bq_client):
        """query_to_arrow forwards BQ query parameters in job config."""
        expected_table = pa.table({"col1": [10]})
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = expected_table
        mock_bq_client.query.return_value = mock_job

        mock_job_config = MagicMock()
        params = [MagicMock()]  # Mock ScalarQueryParameter

        with patch("connectors.bigquery.client.bigquery") as mock_bq_module:
            mock_bq_module.QueryJobConfig.return_value = mock_job_config
            client.client = mock_bq_client

            client.query_to_arrow("SELECT 1 WHERE x > @val", params=params)

        # Verify params were set on the job config
        assert mock_job_config.query_parameters == params

    def test_no_params_does_not_set_query_parameters(self, client, mock_bq_client):
        """When no params given, query_parameters is not set on job config."""
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = pa.table({"x": [1]})
        mock_bq_client.query.return_value = mock_job

        mock_job_config = MagicMock(spec=[])
        with patch("connectors.bigquery.client.bigquery") as mock_bq_module:
            mock_bq_module.QueryJobConfig.return_value = mock_job_config
            client.client = mock_bq_client

            client.query_to_arrow("SELECT 1")

        # query_parameters should not have been set
        assert not hasattr(mock_job_config, "query_parameters") or not getattr(
            mock_job_config, "query_parameters", None
        )


# ---------------------------------------------------------------------------
# 8. read_table builds correct SQL query
# ---------------------------------------------------------------------------

class TestReadTable:
    def test_full_table_select_all(self, client, mock_bq_client):
        """read_table with no columns or filter generates SELECT *."""
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = pa.table({"a": [1]})
        mock_bq_client.query.return_value = mock_job

        client.read_table("proj.dataset.tbl")

        sql = mock_bq_client.query.call_args[0][0]
        assert "SELECT *" in sql
        assert "`proj.dataset.tbl`" in sql
        assert "WHERE" not in sql

    def test_select_specific_columns(self, client, mock_bq_client):
        """read_table with columns list generates SELECT with backtick-quoted names."""
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = pa.table({"a": [1]})
        mock_bq_client.query.return_value = mock_job

        client.read_table("proj.dataset.tbl", columns=["col_a", "col_b"])

        sql = mock_bq_client.query.call_args[0][0]
        assert "`col_a`" in sql
        assert "`col_b`" in sql
        assert "*" not in sql

    def test_with_row_filter(self, client, mock_bq_client):
        """read_table with row_filter appends WHERE clause."""
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = pa.table({"a": [1]})
        mock_bq_client.query.return_value = mock_job

        client.read_table("proj.dataset.tbl", row_filter="status = 'active'")

        sql = mock_bq_client.query.call_args[0][0]
        assert "WHERE status = 'active'" in sql

    def test_columns_and_filter_combined(self, client, mock_bq_client):
        """read_table with both columns and filter generates correct SQL."""
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = pa.table({"x": [1]})
        mock_bq_client.query.return_value = mock_job

        client.read_table(
            "proj.dataset.tbl",
            columns=["id", "name"],
            row_filter="id > 100",
        )

        sql = mock_bq_client.query.call_args[0][0]
        assert "`id`, `name`" in sql
        assert "WHERE id > 100" in sql
        assert "`proj.dataset.tbl`" in sql


# ---------------------------------------------------------------------------
# 9. read_table_incremental builds parameterized WHERE clause
# ---------------------------------------------------------------------------

class TestReadTableIncremental:
    def test_incremental_query_structure(self, client, mock_bq_client):
        """read_table_incremental builds WHERE col > @since_value with params."""
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = pa.table({"a": [1]})
        mock_bq_client.query.return_value = mock_job

        with patch("connectors.bigquery.client.bigquery") as mock_bq_module:
            mock_bq_module.QueryJobConfig.return_value = MagicMock()
            mock_param = MagicMock()
            mock_bq_module.ScalarQueryParameter.return_value = mock_param
            # Re-assign the client's bq client (the fixture already set it up)
            client.client = mock_bq_client

            client.read_table_incremental(
                table_id="proj.dataset.events",
                incremental_column="updated_at",
                since_value="2025-01-01T00:00:00Z",
            )

            sql = mock_bq_client.query.call_args[0][0]
            assert "SELECT *" in sql
            assert "`proj.dataset.events`" in sql
            assert "`updated_at` > @since_value" in sql

            # Verify ScalarQueryParameter was constructed correctly
            mock_bq_module.ScalarQueryParameter.assert_called_once_with(
                "since_value", "TIMESTAMP", "2025-01-01T00:00:00Z"
            )

    def test_incremental_with_columns(self, client, mock_bq_client):
        """read_table_incremental with columns list selects specific columns."""
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = pa.table({"a": [1]})
        mock_bq_client.query.return_value = mock_job

        with patch("connectors.bigquery.client.bigquery") as mock_bq_module:
            mock_bq_module.QueryJobConfig.return_value = MagicMock()
            mock_bq_module.ScalarQueryParameter.return_value = MagicMock()
            client.client = mock_bq_client

            client.read_table_incremental(
                table_id="proj.dataset.events",
                incremental_column="updated_at",
                since_value="2025-01-01T00:00:00Z",
                columns=["id", "name"],
            )

            sql = mock_bq_client.query.call_args[0][0]
            assert "`id`, `name`" in sql
            assert "*" not in sql


# ---------------------------------------------------------------------------
# 10. read_table_partitioned builds correct range query
# ---------------------------------------------------------------------------

class TestReadTablePartitioned:
    def test_partitioned_start_only(self, client, mock_bq_client):
        """With only start, generates >= @start_value without end clause."""
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = pa.table({"a": [1]})
        mock_bq_client.query.return_value = mock_job

        with patch("connectors.bigquery.client.bigquery") as mock_bq_module:
            mock_bq_module.QueryJobConfig.return_value = MagicMock()
            mock_bq_module.ScalarQueryParameter.return_value = MagicMock()
            client.client = mock_bq_client

            client.read_table_partitioned(
                table_id="proj.dataset.events",
                partition_column="event_date",
                start="2025-01-01",
            )

            sql = mock_bq_client.query.call_args[0][0]
            assert "`event_date` >= @start_value" in sql
            assert "@end_value" not in sql

            # Only start_value parameter created
            assert mock_bq_module.ScalarQueryParameter.call_count == 1
            mock_bq_module.ScalarQueryParameter.assert_called_with(
                "start_value", "TIMESTAMP", "2025-01-01"
            )

    def test_partitioned_start_and_end(self, client, mock_bq_client):
        """With start and end, generates >= @start_value AND < @end_value."""
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = pa.table({"a": [1]})
        mock_bq_client.query.return_value = mock_job

        with patch("connectors.bigquery.client.bigquery") as mock_bq_module:
            mock_bq_module.QueryJobConfig.return_value = MagicMock()
            mock_bq_module.ScalarQueryParameter.return_value = MagicMock()
            client.client = mock_bq_client

            client.read_table_partitioned(
                table_id="proj.dataset.events",
                partition_column="event_date",
                start="2025-01-01",
                end="2025-06-01",
            )

            sql = mock_bq_client.query.call_args[0][0]
            assert "`event_date` >= @start_value" in sql
            assert "`event_date` < @end_value" in sql

            # Both start_value and end_value parameters created
            assert mock_bq_module.ScalarQueryParameter.call_count == 2
            calls = mock_bq_module.ScalarQueryParameter.call_args_list
            assert calls[0].args == ("start_value", "TIMESTAMP", "2025-01-01")
            assert calls[1].args == ("end_value", "TIMESTAMP", "2025-06-01")

    def test_partitioned_with_columns(self, client, mock_bq_client):
        """read_table_partitioned with columns selects specific columns."""
        mock_job = MagicMock()
        mock_job.to_arrow.return_value = pa.table({"a": [1]})
        mock_bq_client.query.return_value = mock_job

        with patch("connectors.bigquery.client.bigquery") as mock_bq_module:
            mock_bq_module.QueryJobConfig.return_value = MagicMock()
            mock_bq_module.ScalarQueryParameter.return_value = MagicMock()
            client.client = mock_bq_client

            client.read_table_partitioned(
                table_id="proj.dataset.events",
                partition_column="event_date",
                start="2025-01-01",
                columns=["id", "event_date", "value"],
            )

            sql = mock_bq_client.query.call_args[0][0]
            assert "`id`, `event_date`, `value`" in sql
            assert "*" not in sql


# ---------------------------------------------------------------------------
# 11. test_connection returns True on success, False on failure
# ---------------------------------------------------------------------------

class TestTestConnection:
    def test_returns_true_on_success(self, client, mock_bq_client):
        """test_connection returns True when SELECT 1 query succeeds."""
        mock_job = MagicMock()
        mock_job.result.return_value = iter([(1,)])
        mock_bq_client.query.return_value = mock_job

        assert client.test_connection() is True
        mock_bq_client.query.assert_called_once_with("SELECT 1")

    def test_returns_false_on_failure(self, client, mock_bq_client):
        """test_connection returns False when the query raises an exception."""
        mock_bq_client.query.side_effect = Exception("Connection refused")

        assert client.test_connection() is False

    def test_returns_false_when_result_fails(self, client, mock_bq_client):
        """test_connection returns False when result iteration fails."""
        mock_job = MagicMock()
        mock_job.result.side_effect = Exception("Timeout")
        mock_bq_client.query.return_value = mock_job

        assert client.test_connection() is False


# ---------------------------------------------------------------------------
# 12. Type mapping completeness (all BQ types have PyArrow mapping)
# ---------------------------------------------------------------------------

class TestTypeMapping:
    # All standard BigQuery types that should be mapped
    EXPECTED_BQ_TYPES = [
        "STRING", "BYTES", "INTEGER", "INT64",
        "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC",
        "BOOLEAN", "BOOL",
        "TIMESTAMP", "DATE", "TIME", "DATETIME",
        "GEOGRAPHY", "JSON",
        "STRUCT", "RECORD", "ARRAY",
    ]

    def test_all_standard_bq_types_are_mapped(self):
        """Every standard BigQuery type has an entry in BIGQUERY_TO_PYARROW_TYPES."""
        for bq_type in self.EXPECTED_BQ_TYPES:
            assert bq_type in BIGQUERY_TO_PYARROW_TYPES, (
                f"Missing PyArrow mapping for BQ type: {bq_type}"
            )

    def test_all_mappings_produce_valid_pyarrow_types(self):
        """Every mapped value is a valid PyArrow DataType."""
        for bq_type, pa_type in BIGQUERY_TO_PYARROW_TYPES.items():
            assert isinstance(pa_type, pa.DataType), (
                f"BQ type {bq_type} maps to non-DataType: {pa_type!r}"
            )

    def test_integer_types_map_to_int64(self):
        """Both INTEGER and INT64 map to pa.int64()."""
        assert BIGQUERY_TO_PYARROW_TYPES["INTEGER"] == pa.int64()
        assert BIGQUERY_TO_PYARROW_TYPES["INT64"] == pa.int64()

    def test_float_types_map_to_float64(self):
        """FLOAT, FLOAT64, NUMERIC, BIGNUMERIC all map to pa.float64()."""
        for t in ["FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"]:
            assert BIGQUERY_TO_PYARROW_TYPES[t] == pa.float64()

    def test_boolean_types_map_to_bool(self):
        """Both BOOLEAN and BOOL map to pa.bool_()."""
        assert BIGQUERY_TO_PYARROW_TYPES["BOOLEAN"] == pa.bool_()
        assert BIGQUERY_TO_PYARROW_TYPES["BOOL"] == pa.bool_()

    def test_date_maps_to_date32(self):
        """DATE maps to pa.date32()."""
        assert BIGQUERY_TO_PYARROW_TYPES["DATE"] == pa.date32()

    def test_timestamp_has_utc_timezone(self):
        """TIMESTAMP maps to pa.timestamp with UTC timezone."""
        ts_type = BIGQUERY_TO_PYARROW_TYPES["TIMESTAMP"]
        assert ts_type == pa.timestamp("us", tz="UTC")

    def test_datetime_has_no_timezone(self):
        """DATETIME maps to pa.timestamp without timezone."""
        dt_type = BIGQUERY_TO_PYARROW_TYPES["DATETIME"]
        assert dt_type == pa.timestamp("us")

    def test_complex_types_map_to_string(self):
        """STRUCT, RECORD, ARRAY, GEOGRAPHY, JSON all serialize as string."""
        for t in ["STRUCT", "RECORD", "ARRAY", "GEOGRAPHY", "JSON"]:
            assert BIGQUERY_TO_PYARROW_TYPES[t] == pa.string()


# ---------------------------------------------------------------------------
# 13. Metadata cache save/load from disk
# ---------------------------------------------------------------------------

class TestMetadataCachePersistence:
    def test_save_and_load_cache(self, tmp_path):
        """Metadata cache is persisted to disk and reloaded on new client init."""
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        cache_file = metadata_dir / "bq_table_metadata.json"

        mock_config = MagicMock()
        mock_config.get_metadata_path.return_value = metadata_dir

        # First client: fetch metadata and save to cache
        mock_bq = MagicMock()
        table_id = "proj.ds.tbl"
        schema = [_make_bq_field("col1", "STRING")]
        table_ref = _make_table_ref(table_id=table_id, schema=schema)
        mock_bq.get_table.return_value = table_ref

        with (
            patch("connectors.bigquery.client.bigquery.Client", return_value=mock_bq),
            patch("connectors.bigquery.client.get_config", return_value=mock_config),
            patch.dict("os.environ", {"BIGQUERY_PROJECT": "proj"}),
        ):
            client1 = BigQueryClient()
            client1.get_table_metadata(table_id, use_cache=False)

        # Verify the cache file was written
        assert cache_file.exists()
        saved_data = json.loads(cache_file.read_text())
        assert table_id in saved_data
        assert saved_data[table_id]["columns"] == ["col1"]

        # Second client: loads cache from disk on init
        mock_bq2 = MagicMock()
        with (
            patch("connectors.bigquery.client.bigquery.Client", return_value=mock_bq2),
            patch("connectors.bigquery.client.get_config", return_value=mock_config),
            patch.dict("os.environ", {"BIGQUERY_PROJECT": "proj"}),
        ):
            client2 = BigQueryClient()

        assert table_id in client2.metadata_cache
        assert client2.metadata_cache[table_id]["columns"] == ["col1"]

    def test_load_handles_corrupt_cache_file(self, tmp_path):
        """Client handles corrupt cache JSON gracefully without crashing."""
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        cache_file = metadata_dir / "bq_table_metadata.json"
        cache_file.write_text("{corrupt json!!!")

        mock_config = MagicMock()
        mock_config.get_metadata_path.return_value = metadata_dir

        mock_bq = MagicMock()
        with (
            patch("connectors.bigquery.client.bigquery.Client", return_value=mock_bq),
            patch("connectors.bigquery.client.get_config", return_value=mock_config),
            patch.dict("os.environ", {"BIGQUERY_PROJECT": "proj"}),
        ):
            client = BigQueryClient()

        # Cache should be empty after corrupt file
        assert client.metadata_cache == {}

    def test_load_handles_missing_cache_file(self, tmp_path):
        """Client initializes with empty cache when no cache file exists."""
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        # No cache file created

        mock_config = MagicMock()
        mock_config.get_metadata_path.return_value = metadata_dir

        mock_bq = MagicMock()
        with (
            patch("connectors.bigquery.client.bigquery.Client", return_value=mock_bq),
            patch("connectors.bigquery.client.get_config", return_value=mock_config),
            patch.dict("os.environ", {"BIGQUERY_PROJECT": "proj"}),
        ):
            client = BigQueryClient()

        assert client.metadata_cache == {}

    def test_save_creates_parent_directories(self, tmp_path):
        """_save_metadata_cache creates parent directories if they do not exist."""
        # Use a nested path that does not yet exist
        metadata_dir = tmp_path / "deep" / "nested" / "metadata"
        # Do NOT create directories upfront

        mock_config = MagicMock()
        mock_config.get_metadata_path.return_value = metadata_dir

        mock_bq = MagicMock()
        table_id = "proj.ds.tbl"
        schema = [_make_bq_field("x", "INTEGER")]
        table_ref = _make_table_ref(table_id=table_id, schema=schema)
        mock_bq.get_table.return_value = table_ref

        with (
            patch("connectors.bigquery.client.bigquery.Client", return_value=mock_bq),
            patch("connectors.bigquery.client.get_config", return_value=mock_config),
            patch.dict("os.environ", {"BIGQUERY_PROJECT": "proj"}),
        ):
            client = BigQueryClient()
            client.get_table_metadata(table_id, use_cache=False)

        cache_file = metadata_dir / "bq_table_metadata.json"
        assert cache_file.exists()


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

class TestCreateClient:
    def test_create_client_returns_bigquery_client(self, mock_config):
        """create_client() factory returns a BigQueryClient instance."""
        mock_bq = MagicMock()
        with (
            patch("connectors.bigquery.client.bigquery.Client", return_value=mock_bq),
            patch("connectors.bigquery.client.get_config", return_value=mock_config),
            patch.dict("os.environ", {"BIGQUERY_PROJECT": "factory-project"}),
        ):
            result = create_client()

        assert isinstance(result, BigQueryClient)
        assert result.project_id == "factory-project"
