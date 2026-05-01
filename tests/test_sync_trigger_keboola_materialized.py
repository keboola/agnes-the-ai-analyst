"""Scheduler-level test: when a Keboola row has query_mode='materialized',
_run_materialized_pass dispatches to connectors.keboola.extractor.materialize_query
(not BQ's). Existing BQ-materialized rows continue using BqAccess.

Mirrors the unit-style of tests/test_sync_trigger_materialized.py — patches
the inner extractor entry points instead of going through the API layer.
"""
import duckdb
import pytest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.db import _ensure_schema
from src.repositories.table_registry import TableRegistryRepository
from connectors.bigquery.access import BqAccess, BqProjects


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    yield conn
    conn.close()


@pytest.fixture
def stub_bq():
    @contextmanager
    def _session(_p):
        conn = duckdb.connect(":memory:")
        try:
            yield conn
        finally:
            conn.close()

    return BqAccess(
        BqProjects(billing="t", data="t"),
        client_factory=lambda _p: MagicMock(),
        duckdb_session_factory=_session,
    )


def test_run_materialized_pass_dispatches_keboola_to_keboola_extractor(
    system_db, stub_bq, tmp_path, monkeypatch
):
    """Keboola row with query_mode='materialized' must invoke the Keboola
    materialize_query, not the BQ one."""
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="orders_recent", name="orders_recent",
        source_type="keboola", query_mode="materialized",
        source_query='SELECT * FROM kbc."in.c-sales"."orders" WHERE 1=1',
        sync_schedule="every 1m",  # always due
    )

    # Provide instance.yaml-shape config + env so the Keboola lazy-init succeeds.
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "fake-token")
    from app.api import sync as sync_mod

    # Patch get_value to return the keboola URL/token_env.
    def _fake_get_value(*keys, default=None):
        path = keys
        if path == ("data_source", "keboola", "url"):
            return "https://connection.keboola.com/"
        if path == ("data_source", "keboola", "token_env"):
            return "KEBOOLA_STORAGE_TOKEN"
        if path == ("data_source", "bigquery", "max_bytes_per_materialize"):
            return default if default is not None else 0
        return default

    # Pre-create the parquet for hash bookkeeping (kb materialize is patched
    # so it won't write a real one).
    parquet_dir = tmp_path / "data" / "extracts" / "keboola" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    (parquet_dir / "orders_recent.parquet").write_bytes(
        b"PAR1" + b"\x00" * 16 + b"PAR1"
    )

    bq_called = MagicMock()
    kb_called = MagicMock(return_value={
        "table_id": "orders_recent", "rows": 1, "bytes": 100,
        "md5": "abc123", "path": str(parquet_dir / "orders_recent.parquet"),
    })

    with patch("app.instance_config.get_value", _fake_get_value), \
         patch("connectors.bigquery.extractor.materialize_query", bq_called), \
         patch("connectors.keboola.extractor.materialize_query", kb_called):
        summary = sync_mod._run_materialized_pass(system_db, stub_bq)

    assert kb_called.called, "Keboola materialize_query was not invoked"
    assert not bq_called.called, (
        "BQ materialize_query was wrongly invoked for a Keboola row"
    )
    assert "orders_recent" in summary["materialized"]


def test_run_materialized_pass_dispatches_bigquery_to_bq_extractor(
    system_db, stub_bq, tmp_path
):
    """Regression: BQ-materialized path keeps working unchanged."""
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="events_summary", name="events_summary",
        source_type="bigquery", query_mode="materialized",
        source_query="SELECT date, COUNT(*) FROM `proj.dataset.events` GROUP BY 1",
        sync_schedule="every 1m",
    )

    parquet_dir = tmp_path / "data" / "extracts" / "bigquery" / "data"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    (parquet_dir / "events_summary.parquet").write_bytes(
        b"PAR1" + b"\x00" * 16 + b"PAR1"
    )

    bq_called = MagicMock(return_value={
        "rows": 1, "size_bytes": 100, "query_mode": "materialized",
        "hash": "abc123",
    })
    kb_called = MagicMock()

    from app.api import sync as sync_mod

    with patch("app.api.sync._materialize_table", bq_called), \
         patch("connectors.keboola.extractor.materialize_query", kb_called):
        summary = sync_mod._run_materialized_pass(system_db, stub_bq)

    assert bq_called.called
    assert not kb_called.called
    assert "events_summary" in summary["materialized"]
