"""
Google BigQuery API Client

Low-level wrapper for Google BigQuery with these functions:
1. Authentication using Application Default Credentials (ADC)
2. Query tables to PyArrow (no CSV intermediate step)
3. Get table metadata (schema, columns, data types)
4. Cache metadata for faster repeated use
5. Incremental reads (timestamp-based and partition-based)

Uses google-cloud-bigquery with native PyArrow support.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

import pyarrow as pa
from google.cloud import bigquery

from src.config import get_config


logger = logging.getLogger(__name__)


# Mapping BigQuery types to PyArrow types
BIGQUERY_TO_PYARROW_TYPES = {
    "STRING": pa.string(),
    "BYTES": pa.binary(),
    "INTEGER": pa.int64(),
    "INT64": pa.int64(),
    "FLOAT": pa.float64(),
    "FLOAT64": pa.float64(),
    "NUMERIC": pa.float64(),
    "BIGNUMERIC": pa.float64(),
    "BOOLEAN": pa.bool_(),
    "BOOL": pa.bool_(),
    "TIMESTAMP": pa.timestamp("us", tz="UTC"),
    "DATE": pa.date32(),
    "TIME": pa.string(),
    "DATETIME": pa.timestamp("us"),
    "GEOGRAPHY": pa.string(),
    "JSON": pa.string(),
    "STRUCT": pa.string(),
    "RECORD": pa.string(),
    "ARRAY": pa.string(),
}


class BigQueryClient:
    """
    Wrapper for Google BigQuery API.

    Provides high-level methods for working with BigQuery tables:
    - Query tables to PyArrow Tables (no CSV step)
    - Get metadata (schema, columns)
    - Incremental and partitioned reads
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        location: Optional[str] = None,
    ):
        """
        Initialize BigQuery client.

        Args:
            project_id: GCP project ID for job execution/billing.
                        If None, reads from BIGQUERY_PROJECT env var.
            location: BigQuery location for job execution (e.g., "us-central1").
                      If None, reads from BIGQUERY_LOCATION env var.

        Raises:
            ValueError: If project_id is not provided and BIGQUERY_PROJECT is not set.
        """
        self.project_id = project_id or os.environ.get("BIGQUERY_PROJECT")

        if not self.project_id:
            raise ValueError(
                "BigQuery project ID not set. "
                "Set BIGQUERY_PROJECT environment variable."
            )

        self.location = location or os.environ.get("BIGQUERY_LOCATION")

        # Initialize BigQuery client with ADC
        # project_id is used for job execution and billing.
        # Data can live in a different project -- table IDs in queries
        # use fully-qualified format (project.dataset.table).
        client_kwargs = {"project": self.project_id}
        if self.location:
            client_kwargs["location"] = self.location
        self.client = bigquery.Client(**client_kwargs)

        # Metadata cache
        config = get_config()
        self.metadata_cache: Dict[str, Dict[str, Any]] = {}
        self.metadata_cache_path = config.get_metadata_path() / "bq_table_metadata.json"

        # Load cache from disk if exists
        self._load_metadata_cache()

        logger.info(
            f"BigQuery client initialized: project={self.project_id}, "
            f"location={self.location or 'auto'}"
        )

    def _load_metadata_cache(self):
        """Load metadata cache from disk."""
        if self.metadata_cache_path.exists():
            try:
                with open(self.metadata_cache_path, "r") as f:
                    self.metadata_cache = json.load(f)
                logger.info(
                    f"BQ metadata cache loaded: {len(self.metadata_cache)} tables"
                )
            except Exception as e:
                logger.warning(f"Error loading BQ metadata cache: {e}")
                self.metadata_cache = {}

    def _save_metadata_cache(self):
        """Save metadata cache to disk."""
        try:
            self.metadata_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.metadata_cache_path, "w") as f:
                json.dump(self.metadata_cache, f, indent=2)
            logger.debug("BQ metadata cache saved")
        except Exception as e:
            logger.warning(f"Error saving BQ metadata cache: {e}")

    def get_table_metadata(
        self,
        table_id: str,
        use_cache: bool = True,
        cache_ttl_hours: int = 24,
    ) -> Dict[str, Any]:
        """
        Get table metadata from BigQuery.

        Args:
            table_id: Full table ID (e.g., "project.dataset.table")
            use_cache: Use cache if available
            cache_ttl_hours: Cache TTL in hours (default 24h)

        Returns:
            Dictionary with metadata including columns, types, descriptions, row count.
        """
        # Check cache
        if use_cache and table_id in self.metadata_cache:
            cached = self.metadata_cache[table_id]
            cached_time = datetime.fromisoformat(cached.get("_cached_at", "2000-01-01"))
            cache_age = datetime.now() - cached_time

            if cache_age < timedelta(hours=cache_ttl_hours):
                logger.debug(f"Using BQ metadata cache for {table_id}")
                return cached

        logger.info(f"Fetching metadata for BQ table: {table_id}")

        try:
            table_ref = self.client.get_table(table_id)

            # Build column metadata
            columns = []
            column_types = {}
            column_descriptions = {}
            for field in table_ref.schema:
                columns.append(field.name)
                column_types[field.name] = field.field_type
                if field.description:
                    column_descriptions[field.name] = field.description

            metadata = {
                "table_id": table_id,
                "name": table_ref.table_id,
                "dataset": table_ref.dataset_id,
                "project": table_ref.project,
                "columns": columns,
                "column_types": column_types,
                "column_descriptions": column_descriptions,
                "row_count": table_ref.num_rows,
                "size_bytes": table_ref.num_bytes,
                "created": table_ref.created.isoformat() if table_ref.created else None,
                "modified": table_ref.modified.isoformat() if table_ref.modified else None,
                "partitioning": None,
                "_cached_at": datetime.now().isoformat(),
            }

            # Capture partitioning info
            if table_ref.time_partitioning:
                metadata["partitioning"] = {
                    "type": table_ref.time_partitioning.type_,
                    "field": table_ref.time_partitioning.field,
                    "expiration_ms": table_ref.time_partitioning.expiration_ms,
                }

            # Save to cache
            self.metadata_cache[table_id] = metadata
            self._save_metadata_cache()

            return metadata

        except Exception as e:
            logger.error(f"Error getting metadata for {table_id}: {e}")
            raise

    def get_pyarrow_schema(self, table_id: str) -> Optional[pa.Schema]:
        """
        Build PyArrow schema from BigQuery table schema.

        Args:
            table_id: Full table ID

        Returns:
            PyArrow schema or None if metadata unavailable
        """
        metadata = self.get_table_metadata(table_id)
        column_types = metadata.get("column_types", {})

        if not column_types:
            logger.warning(f"No column types for {table_id}, schema will not be applied")
            return None

        fields = []
        for col_name in metadata.get("columns", []):
            bq_type = column_types.get(col_name, "STRING")
            pa_type = BIGQUERY_TO_PYARROW_TYPES.get(bq_type, pa.string())
            fields.append(pa.field(col_name, pa_type))

        return pa.schema(fields)

    def get_date_columns(self, table_id: str) -> List[str]:
        """
        Get list of DATE-only columns for a table.

        Args:
            table_id: Full table ID

        Returns:
            List of column names that have DATE type in BigQuery
        """
        metadata = self.get_table_metadata(table_id)
        column_types = metadata.get("column_types", {})

        return [
            col_name for col_name, bq_type in column_types.items()
            if bq_type == "DATE"
        ]

    def query_to_arrow(
        self,
        sql: str,
        params: Optional[List[bigquery.ScalarQueryParameter]] = None,
    ) -> pa.Table:
        """
        Execute SQL query and return results as PyArrow Table.

        Args:
            sql: SQL query string (use @param_name for parameterized values)
            params: List of BigQuery query parameters

        Returns:
            PyArrow Table with query results
        """
        job_config = bigquery.QueryJobConfig()
        if params:
            job_config.query_parameters = params

        logger.debug(f"Executing BQ query: {sql[:200]}...")

        query_job = self.client.query(sql, job_config=job_config)
        arrow_table = query_job.to_arrow()

        logger.debug(f"Query returned {arrow_table.num_rows} rows, {arrow_table.num_columns} columns")
        return arrow_table

    def read_table(
        self,
        table_id: str,
        columns: Optional[List[str]] = None,
        row_filter: Optional[str] = None,
    ) -> pa.Table:
        """
        Read full table (or filtered subset) as PyArrow Table.

        Args:
            table_id: Full table ID (e.g., "project.dataset.table")
            columns: Optional list of columns to select
            row_filter: Optional SQL WHERE clause (without WHERE keyword)

        Returns:
            PyArrow Table with table data
        """
        # Build SELECT clause
        select_cols = ", ".join(f"`{c}`" for c in columns) if columns else "*"

        sql = f"SELECT {select_cols} FROM `{table_id}`"
        if row_filter:
            sql += f" WHERE {row_filter}"

        logger.info(f"Reading BQ table: {table_id} (filter: {row_filter or 'none'})")
        return self.query_to_arrow(sql)

    def read_table_incremental(
        self,
        table_id: str,
        incremental_column: str,
        since_value: str,
        columns: Optional[List[str]] = None,
    ) -> pa.Table:
        """
        Read rows where incremental_column > since_value.

        Uses parameterized query to prevent SQL injection.

        Args:
            table_id: Full table ID
            incremental_column: Column name for incremental filter
            since_value: ISO timestamp string - fetch rows after this value
            columns: Optional list of columns to select

        Returns:
            PyArrow Table with incremental data
        """
        select_cols = ", ".join(f"`{c}`" for c in columns) if columns else "*"

        sql = (
            f"SELECT {select_cols} FROM `{table_id}` "
            f"WHERE `{incremental_column}` > @since_value"
        )

        params = [
            bigquery.ScalarQueryParameter("since_value", "TIMESTAMP", since_value),
        ]

        logger.info(
            f"Incremental read: {table_id} WHERE {incremental_column} > {since_value}"
        )
        return self.query_to_arrow(sql, params=params)

    def read_table_partitioned(
        self,
        table_id: str,
        partition_column: str,
        start: str,
        end: Optional[str] = None,
        columns: Optional[List[str]] = None,
    ) -> pa.Table:
        """
        Read data within a partition range.

        Args:
            table_id: Full table ID
            partition_column: Partition column name
            start: Start date/timestamp (inclusive)
            end: End date/timestamp (exclusive). If None, reads to present.
            columns: Optional list of columns to select

        Returns:
            PyArrow Table with partition range data
        """
        select_cols = ", ".join(f"`{c}`" for c in columns) if columns else "*"

        sql = (
            f"SELECT {select_cols} FROM `{table_id}` "
            f"WHERE `{partition_column}` >= @start_value"
        )
        params = [
            bigquery.ScalarQueryParameter("start_value", "TIMESTAMP", start),
        ]

        if end:
            sql += f" AND `{partition_column}` < @end_value"
            params.append(
                bigquery.ScalarQueryParameter("end_value", "TIMESTAMP", end),
            )

        logger.info(
            f"Partitioned read: {table_id} [{start} .. {end or 'now'})"
        )
        return self.query_to_arrow(sql, params=params)

    def discover_all_tables(self, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all tables in the project (or specific dataset).

        Args:
            dataset_id: Optional dataset ID to limit scope

        Returns:
            Normalized list of table dicts with id, name, columns, row_count, etc.
        """
        logger.info(f"Discovering BQ tables (dataset={dataset_id or 'all'})...")

        result = []

        if dataset_id:
            datasets = [self.client.get_dataset(dataset_id)]
        else:
            datasets = list(self.client.list_datasets())

        for dataset in datasets:
            ds_ref = dataset.reference if hasattr(dataset, "reference") else dataset.dataset_id
            ds_id = str(ds_ref)

            try:
                tables = list(self.client.list_tables(ds_ref))
            except Exception as e:
                logger.warning(f"Could not list tables in dataset {ds_id}: {e}")
                continue

            for table_item in tables:
                full_id = f"{table_item.project}.{table_item.dataset_id}.{table_item.table_id}"

                try:
                    table_detail = self.client.get_table(full_id)
                    columns = [f.name for f in table_detail.schema]

                    result.append({
                        "id": full_id,
                        "name": table_item.table_id,
                        "bucket_id": table_item.dataset_id,
                        "bucket_name": table_item.dataset_id,
                        "columns": columns,
                        "row_count": table_detail.num_rows or 0,
                        "size_bytes": table_detail.num_bytes or 0,
                        "primary_key": [],
                        "last_change": (
                            table_detail.modified.isoformat()
                            if table_detail.modified else None
                        ),
                        "last_import": None,
                    })
                except Exception as e:
                    logger.warning(f"Could not get details for {full_id}: {e}")

        logger.info(f"Discovered {len(result)} BQ tables")
        return result

    def test_connection(self) -> bool:
        """
        Test connection to BigQuery.

        Returns:
            True if connection works, False otherwise
        """
        try:
            query_job = self.client.query("SELECT 1")
            list(query_job.result())
            logger.info(f"BigQuery connection OK (project: {self.project_id})")
            return True
        except Exception as e:
            logger.error(f"BigQuery connection test failed: {e}")
            return False


def create_client() -> BigQueryClient:
    """
    Factory function to create BigQuery client.

    Returns:
        BigQueryClient instance
    """
    return BigQueryClient()
