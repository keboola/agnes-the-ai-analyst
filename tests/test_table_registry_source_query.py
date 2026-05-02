"""Repository round-trips source_query column for query_mode='materialized'.

Lives alongside the schema-v20 migration: register() now accepts source_query
as an Optional[str] kwarg, and the column flows through SELECT * via list/get.
"""
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
        source_query=(
            "SELECT date, SUM(revenue) FROM `prj.ds.orders` "
            "WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY) GROUP BY 1"
        ),
        sync_schedule="every 6h",
    )
    row = repo.get("orders_90d")
    assert row is not None
    assert row["query_mode"] == "materialized"
    assert "INTERVAL 90 DAY" in row["source_query"]
    assert row["sync_schedule"] == "every 6h"


def test_register_omitted_source_query_stays_null(repo):
    """Default registrations (Keboola local) must not stamp an empty string."""
    repo.register(id="t1", name="t1", source_type="keboola", query_mode="local")
    row = repo.get("t1")
    assert row is not None
    assert row["source_query"] is None


def test_list_all_includes_source_query(repo):
    repo.register(
        id="m1", name="m1", source_type="bigquery",
        query_mode="materialized", source_query="SELECT 1",
    )
    rows = repo.list_all()
    assert len(rows) == 1
    assert rows[0]["source_query"] == "SELECT 1"


def test_register_updates_source_query_on_conflict(repo):
    """Re-registering the same id must overwrite source_query (admin edit)."""
    repo.register(
        id="m1", name="m1", source_type="bigquery",
        query_mode="materialized", source_query="SELECT 1",
    )
    repo.register(
        id="m1", name="m1", source_type="bigquery",
        query_mode="materialized", source_query="SELECT 2",
    )
    row = repo.get("m1")
    assert row["source_query"] == "SELECT 2"


def test_register_preserves_registered_at_when_supplied(repo):
    """source_query addition must not break the existing registered_at
    preservation contract (admin edits keep the original timestamp)."""
    from datetime import datetime, timezone

    original = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    repo.register(
        id="t", name="t", source_type="bigquery",
        query_mode="materialized", source_query="SELECT 1",
        registered_at=original,
    )
    repo.register(
        id="t", name="t", source_type="bigquery",
        query_mode="materialized", source_query="SELECT 2",
        registered_at=original,
    )
    row = repo.get("t")
    assert row["source_query"] == "SELECT 2"
    # Don't assert exact equality on naive vs aware (DuckDB strips tz);
    # just confirm the year+month+day didn't slide forward to 'now'.
    assert row["registered_at"].year == 2026
    assert row["registered_at"].month == 1
    assert row["registered_at"].day == 1
