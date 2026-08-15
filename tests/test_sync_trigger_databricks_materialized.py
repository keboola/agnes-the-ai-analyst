"""Scheduler-level test: when a Databricks row has query_mode='materialized',
_run_materialized_pass dispatches to the Databricks materialize seam with the
instance's configured warehouse settings. Mirrors the unit-style of
tests/test_sync_trigger_keboola_materialized.py — patches the inner entry
points instead of going through the API layer."""

from unittest.mock import MagicMock, patch

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.table_registry import TableRegistryRepository

_SETTINGS = {
    "host": "https://dbc-test.cloud.databricks.com",
    "warehouse_id": "wh-1",
    "catalog": "main",
    "catalogs": ["main"],
    "token": "tok",
}


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    from src.repositories.sync_state import SyncStateRepository

    monkeypatch.setattr(
        "app.api.sync.table_registry_repo",
        lambda: TableRegistryRepository(conn),
    )
    monkeypatch.setattr(
        "app.api.sync.sync_state_repo",
        lambda: SyncStateRepository(conn),
    )

    yield conn
    conn.close()


def _register_databricks_row(
    conn, name="dbx_orders", source_query="SELECT dim, MEASURE(`Revenue`) FROM `main`.`sales`.`kpis` GROUP BY dim"
):
    TableRegistryRepository(conn).register(
        id=name,
        name=name,
        source_type="databricks",
        bucket="sales",
        source_table="orders",
        query_mode="materialized",
        source_query=source_query,
    )


def test_databricks_row_dispatches_to_databricks_seam(system_db):
    from app.api.sync import _run_materialized_pass

    _register_databricks_row(system_db)
    stats = {"rows": 5, "size_bytes": 100, "hash": "a" * 32, "query_mode": "materialized"}

    with (
        patch(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            return_value=_SETTINGS,
        ),
        patch("connectors.databricks.client.DatabricksStatementClient") as client_cls,
        patch("app.api.sync._materialize_databricks_table", return_value=stats) as seam,
    ):
        summary = _run_materialized_pass(system_db, bq=MagicMock())

    assert summary["materialized"] == ["dbx_orders"]
    assert summary["errors"] == []
    client_cls.assert_called_once_with(host=_SETTINGS["host"], token="tok", warehouse_id="wh-1")
    kwargs = seam.call_args.kwargs
    assert kwargs["table_id"] == "dbx_orders"
    assert kwargs["catalog"] == "main"
    assert kwargs["output_dir"].endswith("extracts/databricks")
    assert "MEASURE" in kwargs["row"]["source_query"]

    # sync_state carries the materialize result for the manifest.
    from src.repositories.sync_state import SyncStateRepository

    state = SyncStateRepository(system_db).get_table_state("dbx_orders")
    assert state["status"] == "ok"
    assert state["rows"] == 5


def test_unconfigured_databricks_reports_per_row_error(system_db):
    from app.api.sync import _run_materialized_pass

    _register_databricks_row(system_db)
    with patch(
        "connectors.databricks.semantic_layer.resolve_databricks_settings",
        return_value=None,
    ):
        summary = _run_materialized_pass(system_db, bq=MagicMock())

    assert summary["materialized"] == []
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["table"] == "dbx_orders"
    assert "not configured" in summary["errors"][0]["error"]


def test_source_filter_scopes_databricks_rows(system_db):
    from app.api.sync import _run_materialized_pass

    _register_databricks_row(system_db)
    with (
        patch(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            return_value=_SETTINGS,
        ),
        patch("connectors.databricks.client.DatabricksStatementClient"),
        patch("app.api.sync._materialize_databricks_table") as seam,
    ):
        summary = _run_materialized_pass(system_db, bq=MagicMock(), source_type="bigquery")

    seam.assert_not_called()
    assert summary["materialized"] == []
    assert summary["skipped"] == [{"table": "dbx_orders", "reason": "source_filter"}]


def test_budget_error_is_aggregated_with_structured_fields(system_db):
    from app.api.sync import _run_materialized_pass
    from connectors.bigquery.extractor import MaterializeBudgetError

    _register_databricks_row(system_db)
    err = MaterializeBudgetError("over cap", table_id="dbx_orders", current=2048, limit=1024)
    with (
        patch(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            return_value=_SETTINGS,
        ),
        patch("connectors.databricks.client.DatabricksStatementClient"),
        patch("app.api.sync._materialize_databricks_table", side_effect=err),
    ):
        summary = _run_materialized_pass(system_db, bq=MagicMock())

    assert summary["materialized"] == []
    entry = summary["errors"][0]
    assert entry["table"] == "dbx_orders"
    assert entry["current"] == 2048
    assert entry["limit"] == 1024
