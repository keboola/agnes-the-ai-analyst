"""Repository round-trips source_query column."""
import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.table_registry import TableRegistryRepository


@pytest.fixture
def repo(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    return TableRegistryRepository(conn)


def test_register_persists_source_query(repo):
    repo.register(
        id="orders_90d",
        name="orders_90d",
        source_type="bigquery",
        query_mode="materialized",
        source_query="SELECT date, SUM(revenue) FROM bq.\"prj.ds.orders\" WHERE date >= current_date - INTERVAL 90 DAY GROUP BY 1",
        sync_schedule="every 6h",
    )
    row = repo.get("orders_90d")
    assert row is not None
    assert row["query_mode"] == "materialized"
    assert "INTERVAL 90 DAY" in row["source_query"]
    assert row["sync_schedule"] == "every 6h"


def test_register_omitted_source_query_stays_null(repo):
    repo.register(id="t1", name="t1", source_type="keboola", query_mode="local")
    row = repo.get("t1")
    assert row is not None
    assert row["source_query"] is None


def test_list_all_includes_source_query(repo):
    repo.register(id="m1", name="m1", source_type="bigquery",
                  query_mode="materialized", source_query="SELECT 1")
    rows = repo.list_all()
    assert len(rows) == 1
    assert rows[0]["source_query"] == "SELECT 1"


def test_register_updates_source_query_on_conflict(repo):
    """ON CONFLICT path also updates source_query."""
    repo.register(id="m1", name="m1", source_type="bigquery",
                  query_mode="materialized", source_query="SELECT 1")
    repo.register(id="m1", name="m1", source_type="bigquery",
                  query_mode="materialized", source_query="SELECT 2")
    row = repo.get("m1")
    assert row["source_query"] == "SELECT 2"
