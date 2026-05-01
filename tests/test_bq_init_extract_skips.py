"""init_extract skips rows with query_mode='materialized'.

Materialized rows are written by the sync trigger pass via
`materialize_query()`; they live as parquets in /data/extracts/bigquery/data/
and surface via the orchestrator's standard local-parquet discovery.
Creating a remote view in extract.duckdb for the same name would shadow
the parquet via cross-source name collision.

Pattern matches `tests/test_bigquery_extractor.py::TestViewVsTableTemplates`
(uses `_CapturingProxy` to wrap a real DuckDB conn and stub BQ-specific calls).
"""
import duckdb
from unittest.mock import MagicMock


class _CapturingProxy:
    """Wraps a real DuckDB connection, captures SQL, stubs BQ-specific calls.

    DuckDBPyConnection.execute is C-level read-only, so we wrap rather than
    monkey-patch. Shape lifted directly from tests/test_bigquery_extractor.py
    to keep stub behavior consistent across the BQ test suite.
    """

    def __init__(self, real_conn, captured: list):
        self._real = real_conn
        self._captured = captured

    def execute(self, sql, *args, **kwargs):
        self._captured.append(sql)
        stripped_u = sql.strip().upper()
        if stripped_u.startswith(("INSTALL ", "LOAD ", "CREATE SECRET")):
            return MagicMock()
        if stripped_u.startswith("ATTACH ") and "BIGQUERY" in stripped_u:
            return MagicMock()
        if stripped_u.startswith("DETACH "):
            return MagicMock()
        if 'FROM bq.' in sql or 'FROM bigquery_query' in sql:
            return MagicMock()
        return self._real.execute(sql, *args, **kwargs)

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_init_extract_skips_materialized_rows(tmp_path, monkeypatch):
    """A registry mix of remote + materialized rows: only the remote row
    gets a `_meta` entry; the materialized row is silently skipped."""
    from connectors.bigquery.extractor import init_extract

    monkeypatch.setattr(
        "connectors.bigquery.extractor.get_metadata_token",
        lambda: "test-token",
    )
    monkeypatch.setattr(
        "connectors.bigquery.extractor._detect_table_type",
        lambda *a, **kw: "BASE TABLE",
    )

    captured: list[str] = []
    real_connect = duckdb.connect

    def spy_connect(*a, **kw):
        return _CapturingProxy(real_connect(*a, **kw), captured)

    monkeypatch.setattr(
        "connectors.bigquery.extractor.duckdb.connect", spy_connect
    )

    configs = [
        {
            "name": "live_orders", "bucket": "dset", "source_table": "live",
            "query_mode": "remote", "description": "",
        },
        {
            "name": "agg_90d", "bucket": "dset", "source_table": "live",
            "query_mode": "materialized",
            "source_query": "SELECT 1",
            "description": "",
        },
    ]
    stats = init_extract(str(tmp_path), "test-project", configs)

    db_path = tmp_path / "extract.duckdb"
    assert db_path.exists(), "extract.duckdb should be written"

    db = duckdb.connect(str(db_path))
    meta = db.execute(
        "SELECT table_name, query_mode FROM _meta ORDER BY table_name"
    ).fetchall()
    db.close()

    assert meta == [("live_orders", "remote")]
    assert stats["tables_registered"] == 1
    # No CREATE VIEW for the materialized row
    assert not any("agg_90d" in s for s in captured if "CREATE" in s.upper())
