"""Repository lookup of registry rows by their BigQuery dataset+source_table.

Used by /api/query's RBAC patch to gate direct `bq."<dataset>"."<source_table>"`
references — every such reference must point at a registered row, otherwise
the caller has bypassed the registry and bypassed RBAC.

Closes part of #160.
"""
import time

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.table_registry import TableRegistryRepository


@pytest.fixture
def repo(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    return TableRegistryRepository(conn)


def test_find_returns_none_when_no_match(repo):
    """Empty registry → None for any path."""
    result = repo.find_by_bq_path("finance", "unit_economics")
    assert result is None


def test_find_returns_none_when_not_bigquery(repo):
    """A keboola row with the same bucket+source_table must NOT be returned —
    find_by_bq_path is BQ-only by contract."""
    repo.register(
        id="kbc.in.c-finance.ue",
        name="ue_kbc",
        source_type="keboola",
        bucket="in.c-finance",
        source_table="ue",
        query_mode="local",
    )
    # Even with the same path strings, this is a Keboola row — must not match.
    assert repo.find_by_bq_path("in.c-finance", "ue") is None


def test_find_returns_single_match(repo):
    """One BQ row matching → return it as a dict."""
    repo.register(
        id="bq.finance.unit_economics",
        name="unit_economics",
        source_type="bigquery",
        bucket="finance",
        source_table="unit_economics",
        query_mode="remote",
    )
    row = repo.find_by_bq_path("finance", "unit_economics")
    assert row is not None
    assert row["id"] == "bq.finance.unit_economics"
    assert row["name"] == "unit_economics"
    assert row["source_type"] == "bigquery"


def test_find_oldest_when_multiple_match(repo):
    """No unique constraint on (source_type, bucket, source_table). When 2+
    rows match, return the oldest by `registered_at` so the result is
    deterministic across calls."""
    from datetime import datetime, timezone, timedelta
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    repo.register(
        id="bq.finance.ue.v1",
        name="ue_v1",
        source_type="bigquery",
        bucket="finance",
        source_table="ue",
        query_mode="remote",
        registered_at=base,
    )
    repo.register(
        id="bq.finance.ue.v2",
        name="ue_v2",
        source_type="bigquery",
        bucket="finance",
        source_table="ue",
        query_mode="remote",
        registered_at=base + timedelta(days=30),  # newer
    )
    row = repo.find_by_bq_path("finance", "ue")
    assert row is not None
    assert row["id"] == "bq.finance.ue.v1", \
        f"expected oldest (ue_v1) to win; got {row['id']}"


def test_find_case_insensitive(repo):
    """BQ identifiers are case-preserving but DuckDB analytics views fold
    unquoted identifiers to lowercase. The lookup must match regardless of
    case so user SQL `SELECT FROM bq.Finance.UE` resolves to the registered
    `(finance, unit_economics)` row."""
    repo.register(
        id="bq.finance.unit_economics",
        name="unit_economics",
        source_type="bigquery",
        bucket="finance",
        source_table="unit_economics",
        query_mode="remote",
    )
    # User SQL might come through with any casing.
    assert repo.find_by_bq_path("FINANCE", "UNIT_ECONOMICS") is not None
    assert repo.find_by_bq_path("Finance", "Unit_Economics") is not None
    assert repo.find_by_bq_path("finance", "unit_economics") is not None


def test_find_excludes_null_bucket_or_source_table(repo):
    """Local rows can have NULL bucket/source_table (e.g. some legacy
    materialized rows). Defensive guard: NULL must never match a non-NULL
    query, so the cross-RBAC check doesn't mismatch a NULL registry row."""
    # Insert a BQ row with NULL bucket via direct SQL since register() defaults
    # source_table to table_name.
    repo.conn.execute(
        """INSERT INTO table_registry (id, name, source_type, bucket, source_table, query_mode, registered_at)
        VALUES ('bq.weird', 'weird', 'bigquery', NULL, NULL, 'remote', current_timestamp)""",
    )
    # Looking up with a real bucket+source_table must NOT match the NULL row
    # (regardless of what `lower(NULL)=lower('x')` evaluates to in DuckDB).
    assert repo.find_by_bq_path("foo", "bar") is None
