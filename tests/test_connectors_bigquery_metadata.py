"""BigQuery metadata provider — 5 paths from spec test plan:
happy / sentinel / VIEW / region-typo / both-paths-fail."""

from unittest.mock import MagicMock, patch

import pytest

from app.api._metadata_models import MetadataRequest, TableMetadata


@pytest.fixture
def req():
    return MetadataRequest(
        table_id="orders", bucket="dwh_base", source_table="orders_2024",
    )


def _bq_with_session(table_storage_rows=None, columns_rows=None,
                     table_storage_raises=None, columns_raises=None,
                     legacy_tables_rows=None, legacy_tables_raises=None,
                     projects_data="data-proj", projects_billing="billing-proj"):
    """Mock `BqAccess` whose `duckdb_session()` returns a context manager
    routing `.execute(...)` based on the inner SQL string."""
    bq = MagicMock()
    bq.projects.data = projects_data
    bq.projects.billing = projects_billing

    def execute(outer_sql, params):
        inner_sql = params[1] if len(params) > 1 else ""
        if "TABLE_STORAGE" in inner_sql:
            if table_storage_raises:
                raise table_storage_raises
            return MagicMock(
                fetchone=lambda: table_storage_rows[0] if table_storage_rows else None,
                fetchall=lambda: table_storage_rows or [],
            )
        if "INFORMATION_SCHEMA.COLUMNS" in inner_sql:
            if columns_raises:
                raise columns_raises
            return MagicMock(
                fetchall=lambda: columns_rows or [],
            )
        if "__TABLES__" in inner_sql:
            if legacy_tables_raises:
                raise legacy_tables_raises
            return MagicMock(
                fetchone=lambda: legacy_tables_rows[0] if legacy_tables_rows else None,
            )
        raise AssertionError(f"unexpected SQL: {inner_sql[:80]}")

    session = MagicMock()
    session.execute.side_effect = execute
    cm = MagicMock()
    cm.__enter__.return_value = session
    cm.__exit__.return_value = False
    bq.duckdb_session.return_value = cm
    return bq


def _location_get_value(*keys, default=None):
    """Mock for `app.instance_config.get_value` matching its multi-positional
    signature. Returns 'us-central1' for the BQ location key, default otherwise.
    Regression-anchored to Devin Review #1: the prior buggy single-string call
    silently dropped the configured location; this fixture intentionally
    requires the correct ('data_source', 'bigquery', 'location') tuple."""
    if keys == ("data_source", "bigquery", "location"):
        return "us-central1"
    return default


def test_happy_path_returns_full_metadata(req, monkeypatch):
    """TABLE_STORAGE returns rows+size, COLUMNS returns partition+cluster."""
    from connectors.bigquery import metadata

    monkeypatch.setattr(
        "connectors.bigquery.metadata.get_value",
        _location_get_value,
        raising=False,
    )

    bq = _bq_with_session(
        table_storage_rows=[(1234567, 5_000_000)],
        columns_rows=[
            ("event_date", "DATE", "NO", "YES", None),
            ("country", "STRING", "YES", "NO", 1),
            ("user_id", "STRING", "NO", "NO", None),
        ],
    )
    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        result = metadata.fetch(req)
    assert result == TableMetadata(
        rows=1234567,
        size_bytes=5_000_000,
        partition_by="event_date",
        clustered_by=["country"],
    )


def test_sentinel_unconfigured_returns_none_no_query(req):
    """`bq.projects.data == ''` → return None before any query."""
    from connectors.bigquery import metadata
    bq = _bq_with_session(projects_data="")
    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        assert metadata.fetch(req) is None
    bq.duckdb_session.assert_not_called()


def test_view_path_returns_metadata_with_null_rows_size(req, monkeypatch):
    """VIEW: TABLE_STORAGE empty + __TABLES__ empty → rows/size = None;
    partition + cluster from COLUMNS still surface."""
    from connectors.bigquery import metadata
    monkeypatch.setattr(
        "connectors.bigquery.metadata.get_value",
        _location_get_value,
        raising=False,
    )
    bq = _bq_with_session(
        table_storage_rows=[],   # view → no row
        legacy_tables_rows=[],   # view also absent from __TABLES__
        columns_rows=[
            ("event_date", "DATE", "NO", "YES", None),
        ],
    )
    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        result = metadata.fetch(req)
    assert result is not None
    assert result.rows is None
    assert result.size_bytes is None
    assert result.partition_by == "event_date"


def test_region_typo_falls_through_to_legacy_tables(req, monkeypatch):
    """TABLE_STORAGE raises (typo'd region) → fall through to __TABLES__."""
    from connectors.bigquery import metadata

    def typo_get_value(*keys, default=None):
        if keys == ("data_source", "bigquery", "location"):
            return "us-central"  # typo!
        return default

    monkeypatch.setattr(
        "connectors.bigquery.metadata.get_value",
        typo_get_value,
        raising=False,
    )
    bq = _bq_with_session(
        table_storage_raises=RuntimeError("Not found: ..."),
        legacy_tables_rows=[(100, 2048)],
        columns_rows=[("event_date", "DATE", "NO", "YES", None)],
    )
    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        result = metadata.fetch(req)
    assert result is not None
    assert result.rows == 100
    assert result.size_bytes == 2048


def test_both_paths_fail_returns_metadata_with_partition_only(req, monkeypatch):
    """Both TABLE_STORAGE and __TABLES__ fail → rows/size None, partition still fills."""
    from connectors.bigquery import metadata
    monkeypatch.setattr(
        "connectors.bigquery.metadata.get_value",
        _location_get_value,
        raising=False,
    )
    bq = _bq_with_session(
        table_storage_raises=RuntimeError("BQ down"),
        legacy_tables_raises=RuntimeError("BQ still down"),
        columns_rows=[("event_date", "DATE", "NO", "YES", None)],
    )
    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        result = metadata.fetch(req)
    assert result is not None
    assert result.rows is None
    assert result.size_bytes is None
    assert result.partition_by == "event_date"


def test_location_config_uses_multi_positional_get_value_args(req, monkeypatch):
    """Devin Review #1 regression: `get_value` was called with a single
    dot-separated string `'data_source.bigquery.location'`, but the function
    iterates over separate positional keys — so the call always returned None
    and the BQ location config was never read.

    This test records every call to `get_value` and asserts that the location
    lookup goes through the correct multi-positional form
    (`'data_source', 'bigquery', 'location'`)."""
    from connectors.bigquery import metadata

    calls: list[tuple] = []

    def recording_get_value(*keys, default=None):
        calls.append(keys)
        if keys == ("data_source", "bigquery", "location"):
            return "europe-west1"
        return default

    monkeypatch.setattr(
        "connectors.bigquery.metadata.get_value",
        recording_get_value,
        raising=False,
    )

    captured: dict = {}

    def execute(outer_sql, params):
        if "TABLE_STORAGE" in (params[1] if len(params) > 1 else ""):
            captured["table_storage_sql"] = params[1]
            return MagicMock(fetchone=lambda: (5, 10))
        return MagicMock(fetchall=lambda: [], fetchone=lambda: None)

    bq = MagicMock()
    bq.projects.data = "data-proj"
    bq.projects.billing = "billing-proj"
    session = MagicMock()
    session.execute.side_effect = execute
    cm = MagicMock()
    cm.__enter__.return_value = session
    cm.__exit__.return_value = False
    bq.duckdb_session.return_value = cm

    with patch("connectors.bigquery.metadata.get_bq_access", return_value=bq):
        metadata.fetch(req)

    # The fix: `get_value("data_source", "bigquery", "location")` must appear.
    assert ("data_source", "bigquery", "location") in calls, (
        f"expected ('data_source','bigquery','location') tuple in get_value "
        f"calls, got: {calls}"
    )
    # And the configured location must reach the TABLE_STORAGE SQL — proving
    # the value was actually consumed, not just looked up.
    assert "region-europe-west1" in captured.get("table_storage_sql", ""), (
        f"location config was not propagated to BQ SQL: "
        f"{captured.get('table_storage_sql', '<no SQL captured>')}"
    )


def test_bq_access_error_returns_none(req):
    """get_bq_access() raises BqAccessError → return None gracefully."""
    from connectors.bigquery import metadata
    from connectors.bigquery.access import BqAccessError
    with patch(
        "connectors.bigquery.metadata.get_bq_access",
        side_effect=BqAccessError("not_configured", "not configured"),
    ):
        assert metadata.fetch(req) is None
