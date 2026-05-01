"""materialize_query refuses to run when dry-run estimate exceeds the cap.

The cap is wired through `data_source.bigquery.max_bytes_per_materialize`
(read by the trigger pass; default 10 GiB; set 0 to disable). The dry-run
itself reuses `app.api.v2_scan._bq_dry_run_bytes` so cost-estimate logic
lives in exactly one place. Fail-open behaviour (DuckDB-syntax SQL the
native BQ client can't parse → estimate=0 → COPY proceeds with a warning)
is documented and exercised here too.
"""
import duckdb
import pytest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from connectors.bigquery.access import BqAccess, BqProjects
from connectors.bigquery.extractor import materialize_query, MaterializeBudgetError


def _bq_with_seed(tables: dict[str, str] | None = None) -> BqAccess:
    """Stub BqAccess seeded with in-memory tables (same recipe as
    test_bq_materialize)."""
    tables = tables or {}

    @contextmanager
    def _session(_projects):
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("ATTACH ':memory:' AS bq")
            for s in {ref.rsplit(".", 1)[0] for ref in tables}:
                conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
            for ref, body in tables.items():
                conn.execute(f"CREATE OR REPLACE TABLE {ref} AS {body}")
            yield conn
        finally:
            conn.close()

    return BqAccess(
        BqProjects(billing="test-billing", data="test-data"),
        client_factory=lambda _p: MagicMock(),
        duckdb_session_factory=_session,
    )


def test_refuses_when_estimate_exceeds_cap(tmp_path):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    bq = _bq_with_seed({"bq.test.tiny": "SELECT 1 AS n"})

    with patch(
        "app.api.v2_scan._bq_dry_run_bytes", return_value=100 * 2**30
    ):
        with pytest.raises(MaterializeBudgetError) as exc:
            materialize_query(
                table_id="huge",
                sql="SELECT * FROM bq.test.tiny",
                bq=bq,
                output_dir=str(out),
                max_bytes=10 * 2**30,
            )
    err = exc.value
    assert err.table_id == "huge"
    assert err.current == 100 * 2**30
    assert err.limit == 10 * 2**30


def test_proceeds_when_estimate_under_cap(tmp_path):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    bq = _bq_with_seed({"bq.test.tiny": "SELECT 1 AS n"})

    with patch("app.api.v2_scan._bq_dry_run_bytes", return_value=1024):
        stats = materialize_query(
            table_id="tiny",
            sql="SELECT * FROM bq.test.tiny",
            bq=bq,
            output_dir=str(out),
            max_bytes=10 * 2**30,
        )
    assert stats["rows"] == 1


def test_no_cap_skips_dry_run(tmp_path):
    """When max_bytes=None (default), no dry-run is performed."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    bq = _bq_with_seed({"bq.test.tiny": "SELECT 1 AS n"})

    with patch("app.api.v2_scan._bq_dry_run_bytes") as mock_dry:
        stats = materialize_query(
            table_id="t1",
            sql="SELECT * FROM bq.test.tiny",
            bq=bq,
            output_dir=str(out),
        )
    mock_dry.assert_not_called()
    assert stats["rows"] == 1


def test_zero_max_bytes_skips_dry_run(tmp_path):
    """Sentinel: max_bytes=0 disables the guardrail (config docs)."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    bq = _bq_with_seed({"bq.test.tiny": "SELECT 1 AS n"})

    with patch("app.api.v2_scan._bq_dry_run_bytes") as mock_dry:
        stats = materialize_query(
            table_id="t1",
            sql="SELECT * FROM bq.test.tiny",
            bq=bq,
            output_dir=str(out),
            max_bytes=0,
        )
    mock_dry.assert_not_called()
    assert stats["rows"] == 1


def test_dry_run_failure_is_fail_open(tmp_path):
    """If the dry-run errors (DuckDB syntax, missing google lib, transient
    upstream failure) we don't block — log + proceed with COPY. Operators
    who need hard-fail watch logs for the warning."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    bq = _bq_with_seed({"bq.test.tiny": "SELECT 1 AS n"})

    with patch(
        "app.api.v2_scan._bq_dry_run_bytes", side_effect=RuntimeError("boom")
    ):
        stats = materialize_query(
            table_id="t1",
            sql="SELECT * FROM bq.test.tiny",
            bq=bq,
            output_dir=str(out),
            max_bytes=10 * 2**30,
        )
    assert stats["rows"] == 1
