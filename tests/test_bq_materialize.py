"""BigQuery `materialize_query` writes parquet via BqAccess + DuckDB COPY.

The function takes a `BqAccess` instance so the BQ extension session and
SECRET token live in one place across the codebase (cf. `v2_scan` / `v2_sample`
/ `v2_schema`). Tests inject a stub BqAccess whose `duckdb_session()` yields
an in-memory connection with a pre-attached `bq` catalog containing fixture
tables, exercising the COPY path end-to-end without any GCP traffic.
"""
import duckdb
import pytest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

from connectors.bigquery.access import BqAccess, BqProjects
from connectors.bigquery.extractor import materialize_query, MaterializeBudgetError


def _make_stub_bq(tables: dict[str, str] | None = None) -> BqAccess:
    """Return a BqAccess wired to factories that yield an in-memory DuckDB
    with a pretend `bq` catalog containing test tables. `tables` maps
    DuckDB-three-part references like `'bq.test.orders'` to a SELECT
    expression to seed them with.
    """
    tables = tables or {}

    @contextmanager
    def _session(_projects):
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("ATTACH ':memory:' AS bq")
            schemas = {ref.rsplit(".", 1)[0] for ref in tables}
            for s in schemas:
                conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
            for ref, body in tables.items():
                conn.execute(f"CREATE OR REPLACE TABLE {ref} AS {body}")
            yield conn
        finally:
            conn.close()

    # client_factory returns a stub whose .query(sql, job_config=...) yields
    # a job whose .total_bytes_processed defaults to 0 (fail-open).
    def _client(_projects):
        client = MagicMock()
        job = MagicMock()
        job.total_bytes_processed = 0
        client.query.return_value = job
        return client

    return BqAccess(
        BqProjects(billing="test-billing", data="test-data"),
        client_factory=_client,
        duckdb_session_factory=_session,
    )


def test_materialize_writes_parquet_and_returns_stats(tmp_path):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    bq = _make_stub_bq({
        "bq.test.orders": (
            "SELECT 'EU' AS region, 100 AS revenue UNION ALL "
            "SELECT 'US' AS region, 250 AS revenue"
        )
    })

    stats = materialize_query(
        table_id="orders_summary",
        sql="SELECT region, SUM(revenue) AS revenue FROM bq.test.orders GROUP BY 1",
        bq=bq,
        output_dir=str(out),
    )

    parquet_path = out / "data" / "orders_summary.parquet"
    assert parquet_path.exists()
    assert stats["rows"] == 2
    assert stats["size_bytes"] > 0
    assert stats["query_mode"] == "materialized"

    # Parquet readable end-to-end
    rows = duckdb.connect().execute(
        f"SELECT region, revenue FROM read_parquet('{parquet_path}') ORDER BY region"
    ).fetchall()
    assert rows == [("EU", 100), ("US", 250)]


def test_materialize_atomic_on_failure(tmp_path):
    """Bad SQL must not leave a half-written parquet behind."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    parquet_path = out / "data" / "broken.parquet"

    bq = _make_stub_bq({"bq.test.orders": "SELECT 1 AS n"})

    with pytest.raises(Exception):
        materialize_query(
            table_id="broken",
            sql="SELECT * FROM bq.test.does_not_exist",
            bq=bq,
            output_dir=str(out),
        )
    assert not parquet_path.exists()
    # Tmp also cleaned
    assert not (out / "data" / "broken.parquet.tmp").exists()


def test_materialize_rejects_unsafe_table_id(tmp_path):
    """table_id becomes the parquet filename — block path traversal up front."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    bq = _make_stub_bq()

    with pytest.raises(ValueError, match="unsafe"):
        materialize_query(
            table_id="../etc/passwd",
            sql="SELECT 1",
            bq=bq,
            output_dir=str(out),
        )


def test_materialize_overwrites_existing_parquet(tmp_path):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    bq = _make_stub_bq({"bq.test.tiny": "SELECT 1 AS n"})

    materialize_query(
        table_id="t1", sql="SELECT 1 AS n",
        bq=bq, output_dir=str(out),
    )
    materialize_query(
        table_id="t1", sql="SELECT 2 AS n",
        bq=bq, output_dir=str(out),
    )
    rows = duckdb.connect().execute(
        f"SELECT n FROM read_parquet('{out}/data/t1.parquet')"
    ).fetchall()
    assert rows == [(2,)]
