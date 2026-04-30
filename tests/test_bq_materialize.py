"""BigQuery materialize_query writes parquet via DuckDB COPY.

CI doesn't actually attach BigQuery — we substitute the BQ ATTACH with
an in-memory schema the test sets up, so the COPY pathway is exercised
end-to-end without a network call. The `skip_attach=True` flag on
materialize_query() is the test-only escape hatch.
"""
import duckdb
import pytest

from connectors.bigquery.extractor import materialize_query


@pytest.fixture
def stub_bq(monkeypatch):
    """Replace duckdb.connect so the connection used by materialize_query
    has a pretend bq.test schema with rows we can SELECT against."""
    real_connect = duckdb.connect

    def _stub_connect(path=":memory:"):
        conn = real_connect(path)
        # Only inject the fake BQ catalog into in-memory connections — those
        # are the ones materialize_query opens. File-backed connections (e.g.
        # a real extract.duckdb) are returned as-is.
        if path == ":memory:":
            # Attach a second in-memory DB as alias "bq" so three-part names
            # like bq.test.orders resolve correctly without the BigQuery extension.
            conn.execute("ATTACH ':memory:' AS bq")
            conn.execute("CREATE SCHEMA bq.test")
            conn.execute(
                "CREATE TABLE bq.test.orders AS "
                "SELECT 'EU' AS region, 100 AS revenue UNION ALL "
                "SELECT 'US' AS region, 250 AS revenue"
            )
        return conn

    monkeypatch.setattr(duckdb, "connect", _stub_connect)
    yield


def test_materialize_writes_parquet_and_returns_stats(tmp_path, stub_bq):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    stats = materialize_query(
        table_id="orders_summary",
        sql="SELECT region, SUM(revenue) AS revenue FROM bq.test.orders GROUP BY 1",
        project_id="test-project",
        output_dir=str(out),
        skip_attach=True,
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


def test_materialize_atomic_on_failure(tmp_path, stub_bq):
    """Bad SQL must not leave a half-written parquet behind."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    parquet_path = out / "data" / "broken.parquet"

    with pytest.raises(Exception):
        materialize_query(
            table_id="broken",
            sql="SELECT * FROM bq.test.does_not_exist",
            project_id="test-project",
            output_dir=str(out),
            skip_attach=True,
        )
    assert not parquet_path.exists()


def test_materialize_rejects_unsafe_table_id(tmp_path, stub_bq):
    """table_id becomes the parquet filename — block path traversal up front."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    with pytest.raises(ValueError, match="unsafe"):
        materialize_query(
            table_id="../etc/passwd",
            sql="SELECT 1",
            project_id="test-project",
            output_dir=str(out),
            skip_attach=True,
        )


def test_materialize_overwrites_existing_parquet(tmp_path, stub_bq):
    """Re-running a materialized table replaces the previous parquet."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    materialize_query(
        table_id="t1", sql="SELECT 1 AS n",
        project_id="p", output_dir=str(out), skip_attach=True,
    )
    materialize_query(
        table_id="t1", sql="SELECT 2 AS n",
        project_id="p", output_dir=str(out), skip_attach=True,
    )
    rows = duckdb.connect().execute(
        f"SELECT n FROM read_parquet('{out}/data/t1.parquet')"
    ).fetchall()
    assert rows == [(2,)]
