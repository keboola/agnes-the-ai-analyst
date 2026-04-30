"""_run_materialized_pass walks table_registry for materialized BQ rows
and runs each that is due via materialize_query()."""
import duckdb
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

from src.db import _ensure_schema
from src.repositories.table_registry import TableRegistryRepository
from src.repositories.sync_state import SyncStateRepository


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    yield conn
    conn.close()


def test_materialized_pass_calls_materialize_for_due_rows(system_db, tmp_path):
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="orders_90d", name="orders_90d",
        source_type="bigquery", query_mode="materialized",
        source_query="SELECT 1 AS n",
        sync_schedule="every 1m",  # always due in tests (no prior sync)
    )

    from app.api import sync as sync_mod

    with patch("app.api.sync._materialize_table") as mock_mat:
        mock_mat.return_value = {"rows": 1, "size_bytes": 100, "query_mode": "materialized"}
        summary = sync_mod._run_materialized_pass(
            system_db, project_id="test-project", max_bytes=10 * 2**30,
        )

    mock_mat.assert_called_once()
    call_kwargs = mock_mat.call_args.kwargs
    assert call_kwargs["table_id"] == "orders_90d"
    assert "SELECT 1 AS n" in call_kwargs["sql"]
    assert call_kwargs["project_id"] == "test-project"
    assert call_kwargs["max_bytes"] == 10 * 2**30
    assert "orders_90d" in summary["materialized"]
    assert not summary["errors"]


def test_materialized_pass_skips_undue_rows(system_db, tmp_path):
    repo = TableRegistryRepository(system_db)
    repo.register(
        id="orders_daily", name="orders_daily",
        source_type="bigquery", query_mode="materialized",
        source_query="SELECT 1",
        sync_schedule="daily 03:00",
    )
    # Pretend it ran 5 minutes ago — daily schedule would not be due yet.
    state = SyncStateRepository(system_db)
    state.update_sync(table_id="orders_daily", rows=1, file_size_bytes=10, hash="x")

    from app.api import sync as sync_mod

    with patch("app.api.sync._materialize_table") as mock_mat:
        summary = sync_mod._run_materialized_pass(
            system_db, project_id="p", max_bytes=None,
        )

    mock_mat.assert_not_called()
    assert "orders_daily" in summary["skipped"]


def test_materialized_pass_skips_non_materialized_rows(system_db, tmp_path):
    repo = TableRegistryRepository(system_db)
    repo.register(id="t1", name="t1", source_type="keboola", query_mode="local")
    repo.register(id="t2", name="t2", source_type="bigquery", query_mode="remote")

    from app.api import sync as sync_mod

    with patch("app.api.sync._materialize_table") as mock_mat:
        summary = sync_mod._run_materialized_pass(
            system_db, project_id="p", max_bytes=None,
        )

    mock_mat.assert_not_called()
    assert summary["materialized"] == []
    assert summary["skipped"] == []
    assert summary["errors"] == []


def test_materialized_pass_collects_errors_per_row(system_db, tmp_path):
    """When one row fails, others still proceed; errors aggregated."""
    repo = TableRegistryRepository(system_db)
    repo.register(id="ok", name="ok", source_type="bigquery",
                  query_mode="materialized", source_query="SELECT 1",
                  sync_schedule="every 1m")
    repo.register(id="bad", name="bad", source_type="bigquery",
                  query_mode="materialized", source_query="SELECT broken",
                  sync_schedule="every 1m")

    from app.api import sync as sync_mod

    def _fake_materialize(table_id, sql, project_id, output_dir, max_bytes):
        if table_id == "bad":
            raise RuntimeError("simulated COPY failure")
        return {"rows": 1, "size_bytes": 100, "query_mode": "materialized"}

    with patch("app.api.sync._materialize_table", side_effect=_fake_materialize):
        summary = sync_mod._run_materialized_pass(
            system_db, project_id="p", max_bytes=None,
        )

    assert summary["materialized"] == ["ok"]
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["table"] == "bad"
    assert "simulated" in summary["errors"][0]["error"]


def test_materialized_pass_updates_sync_state_on_success(system_db, tmp_path):
    repo = TableRegistryRepository(system_db)
    repo.register(id="t1", name="t1", source_type="bigquery",
                  query_mode="materialized", source_query="SELECT 1",
                  sync_schedule="every 1m")

    from app.api import sync as sync_mod

    with patch("app.api.sync._materialize_table",
               return_value={"rows": 42, "size_bytes": 1000, "query_mode": "materialized"}):
        sync_mod._run_materialized_pass(system_db, project_id="p", max_bytes=None)

    state = SyncStateRepository(system_db)
    last = state.get_table_state("t1")
    assert last is not None
    assert last["rows"] == 42
    assert last["file_size_bytes"] == 1000
