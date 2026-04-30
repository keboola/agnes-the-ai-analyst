"""init_extract creates remote views only for query_mode='remote' rows;
materialized rows are written by the sync trigger pass instead."""
import duckdb
import pytest

from connectors.bigquery.extractor import init_extract


@pytest.fixture
def stub_bq(monkeypatch):
    """Stub duckdb.connect so the temp extract.duckdb has a fake bq.dset.live
    table to point CREATE VIEW at."""
    real_connect = duckdb.connect

    def _stub(path):
        conn = real_connect(path)
        try:
            conn.execute("ATTACH ':memory:' AS bq")
            conn.execute("CREATE SCHEMA bq.dset")
            conn.execute("CREATE OR REPLACE TABLE bq.dset.live AS SELECT 1 AS x")
        except duckdb.CatalogException:
            pass
        return conn

    monkeypatch.setattr(duckdb, "connect", _stub)
    yield


def test_init_extract_skips_materialized(tmp_path, stub_bq):
    out = tmp_path / "extracts" / "bigquery"

    configs = [
        {"name": "live_orders", "bucket": "dset", "source_table": "live",
         "query_mode": "remote"},
        {"name": "agg_90d", "bucket": "dset", "source_table": "live",
         "query_mode": "materialized", "source_query": "SELECT 1"},
    ]
    stats = init_extract(str(out), "test-project", configs, skip_attach=True)

    db = duckdb.connect(str(out / "extract.duckdb"))
    meta = db.execute(
        "SELECT table_name, query_mode FROM _meta ORDER BY table_name"
    ).fetchall()
    db.close()

    # Only the remote row was registered; the materialized row was skipped.
    assert meta == [("live_orders", "remote")]
    # tables_registered count reflects the actual writes (1, not 2)
    assert stats["tables_registered"] == 1


def test_init_extract_with_only_materialized_creates_empty_meta(tmp_path, stub_bq):
    """When all rows are materialized, _meta is empty but extract.duckdb
    still exists (so the orchestrator scan doesn't break)."""
    out = tmp_path / "extracts" / "bigquery"

    configs = [
        {"name": "agg_90d", "bucket": "dset", "source_table": "live",
         "query_mode": "materialized", "source_query": "SELECT 1"},
    ]
    stats = init_extract(str(out), "test-project", configs, skip_attach=True)

    assert stats["tables_registered"] == 0
    db = duckdb.connect(str(out / "extract.duckdb"))
    rows = db.execute("SELECT count(*) FROM _meta").fetchone()[0]
    db.close()
    assert rows == 0
