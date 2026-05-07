"""Asserts that /api/v2/schema/{id} for a BQ row makes exactly ONE
bigquery_query() call on cache miss, down from two pre-#155.

Counts via a side-effect tracker on the mocked DuckDB session.
"""

from unittest.mock import MagicMock, patch
import pytest


def _mock_duckdb_session_returning(rows):
    """Build a context-manager mock that returns `rows` on .fetchall().

    Exposes `call_count` on the returned mock for assertion.
    """
    session = MagicMock()
    session.execute.return_value.fetchall.return_value = rows
    cm = MagicMock()
    cm.__enter__.return_value = session
    cm.__exit__.return_value = False
    return cm, session


def test_fetch_bq_columns_full_is_single_query():
    """The new shared helper makes exactly ONE call to bigquery_query."""
    from connectors.bigquery.access import fetch_bq_columns_full

    bq = MagicMock()
    bq.projects.data = "data-proj"
    bq.projects.billing = "billing-proj"
    cm, session = _mock_duckdb_session_returning([
        ("event_date", "DATE", "NO", "YES", None),
        ("country", "STRING", "YES", "NO", 1),
        ("user_id", "STRING", "NO", "NO", None),
    ])
    bq.duckdb_session.return_value = cm

    rows = fetch_bq_columns_full(bq, "dwh_base", "events")
    assert len(rows) == 3
    # Exactly one bigquery_query() call — no second round-trip.
    assert session.execute.call_count == 1
    first_call = session.execute.call_args_list[0]
    # Outer wrapper SQL is bigquery_query(?, ?, ?)
    assert "bigquery_query" in first_call.args[0]
    # Inner BQ SQL pulls all five columns we need at once.
    inner_sql = first_call.args[1][1]
    assert "column_name" in inner_sql
    assert "data_type" in inner_sql
    assert "is_nullable" in inner_sql
    assert "is_partitioning_column" in inner_sql
    assert "clustering_ordinal_position" in inner_sql


def test_fetch_bq_columns_full_returns_dicts():
    """Each row is a dict with the documented keys."""
    from connectors.bigquery.access import fetch_bq_columns_full

    bq = MagicMock()
    bq.projects.data = "data-proj"
    bq.projects.billing = "billing-proj"
    cm, _ = _mock_duckdb_session_returning([
        ("event_date", "DATE", "NO", "YES", None),
    ])
    bq.duckdb_session.return_value = cm

    rows = fetch_bq_columns_full(bq, "dwh_base", "events")
    assert rows == [{
        "name": "event_date",
        "type": "DATE",
        "nullable": False,
        "is_partitioning_column": True,
        "clustering_ordinal_position": None,
    }]


def test_fetch_bq_columns_full_returns_none_when_unconfigured():
    """Sentinel BqAccess (data project empty) → return None, no query."""
    from connectors.bigquery.access import fetch_bq_columns_full

    bq = MagicMock()
    bq.projects.data = ""  # sentinel
    rows = fetch_bq_columns_full(bq, "dwh_base", "events")
    assert rows is None
    bq.duckdb_session.assert_not_called()


def test_fetch_bq_columns_full_returns_none_on_unsafe_identifier():
    """Refuses to interpolate identifiers that fail validation."""
    from connectors.bigquery.access import fetch_bq_columns_full

    bq = MagicMock()
    bq.projects.data = "data-proj"
    rows = fetch_bq_columns_full(bq, "evil`; DROP--", "events")
    assert rows is None
    bq.duckdb_session.assert_not_called()


def test_fetch_bq_columns_full_returns_none_on_query_error():
    """BQ failure → log + None; never raises."""
    from connectors.bigquery.access import fetch_bq_columns_full

    bq = MagicMock()
    bq.projects.data = "data-proj"
    bq.projects.billing = "billing-proj"
    cm = MagicMock()
    cm.__enter__.return_value.execute.side_effect = RuntimeError("BQ down")
    cm.__exit__.return_value = False
    bq.duckdb_session.return_value = cm

    rows = fetch_bq_columns_full(bq, "dwh_base", "events")
    assert rows is None
