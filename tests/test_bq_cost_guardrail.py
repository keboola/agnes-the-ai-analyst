"""materialize_query refuses to run when dry-run estimate exceeds the cap."""
import duckdb
import pytest
from unittest.mock import patch

from connectors.bigquery.extractor import materialize_query, MaterializeBudgetError


@pytest.fixture
def stub_bq(monkeypatch):
    real_connect = duckdb.connect

    def _stub(path=":memory:"):
        conn = real_connect(path)
        if path == ":memory:":
            try:
                conn.execute("ATTACH ':memory:' AS bq")
                conn.execute("CREATE SCHEMA bq.test")
                conn.execute(
                    "CREATE OR REPLACE TABLE bq.test.tiny AS SELECT 1 AS n"
                )
            except duckdb.CatalogException:
                pass  # already attached
        return conn

    monkeypatch.setattr(duckdb, "connect", _stub)
    yield


def test_refuses_when_estimate_exceeds_cap(tmp_path, stub_bq):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    with patch("connectors.bigquery.extractor._dry_run_bytes",
               return_value=100 * 2**30):  # 100 GB estimated
        with pytest.raises(MaterializeBudgetError) as exc:
            materialize_query(
                table_id="huge",
                sql="SELECT * FROM bq.test.tiny",
                project_id="p",
                output_dir=str(out),
                max_bytes=10 * 2**30,  # 10 GB cap
                skip_attach=True,
            )
    msg = str(exc.value)
    assert "huge" in msg
    # Bytes appear in message so operators know how much over they were
    assert "10" in msg or "100" in msg


def test_proceeds_when_estimate_under_cap(tmp_path, stub_bq):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    with patch("connectors.bigquery.extractor._dry_run_bytes",
               return_value=1024):
        stats = materialize_query(
            table_id="tiny",
            sql="SELECT * FROM bq.test.tiny",
            project_id="p",
            output_dir=str(out),
            max_bytes=10 * 2**30,
            skip_attach=True,
        )
    assert stats["rows"] == 1
    assert (out / "data" / "tiny.parquet").exists()


def test_no_cap_skips_dry_run(tmp_path, stub_bq):
    """When max_bytes=None (default), no dry-run is performed — preserves
    backwards compat for callers who don't want the guardrail (or for
    test environments without google-cloud-bigquery available)."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    with patch("connectors.bigquery.extractor._dry_run_bytes") as mock_dry:
        stats = materialize_query(
            table_id="t1",
            sql="SELECT * FROM bq.test.tiny",
            project_id="p",
            output_dir=str(out),
            skip_attach=True,
        )
    mock_dry.assert_not_called()
    assert stats["rows"] == 1


def test_dry_run_failure_is_fail_open(tmp_path, stub_bq):
    """If the dry-run itself errors (e.g. google-cloud-bigquery not installed
    in the runtime, or transient API failure), we don't block the operator —
    we proceed and let the actual COPY surface a clearer error.

    This is the documented fail-open behavior; operators who want hard-fail
    set max_bytes high enough to never trigger combined with monitoring."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    with patch("connectors.bigquery.extractor._dry_run_bytes",
               return_value=0):  # 0 = unknown / dry-run failed
        stats = materialize_query(
            table_id="t1",
            sql="SELECT * FROM bq.test.tiny",
            project_id="p",
            output_dir=str(out),
            max_bytes=10 * 2**30,
            skip_attach=True,
        )
    assert stats["rows"] == 1
