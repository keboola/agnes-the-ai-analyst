"""Pydantic-level conflict-policy validation for v26 register fields.

Tests RegisterTableRequest's strategy + filters + partition validators
directly. Avoids the HTTP TestClient surface — model construction +
ValidationError is sufficient signal for the validator-only rules.
"""
import pytest
from pydantic import ValidationError

from app.api.admin import RegisterTableRequest


def _base(**overrides):
    p = {
        "name": "tbl",
        "source_type": "keboola",
        "bucket": "in.c-x",
        "source_table": "tbl",
        "query_mode": "local",
    }
    p.update(overrides)
    return p


# ───────────────────────────── strategy validation ────────────────────────────


def test_full_refresh_default_accepted():
    req = RegisterTableRequest(**_base())
    assert req.sync_strategy == "full_refresh"


def test_incremental_strategy_accepted():
    req = RegisterTableRequest(**_base(
        sync_strategy="incremental",
        primary_key=["id"],
        incremental_window_days=1,
    ))
    assert req.sync_strategy == "incremental"
    assert req.incremental_window_days == 1


def test_partitioned_strategy_accepted():
    req = RegisterTableRequest(**_base(
        sync_strategy="partitioned",
        primary_key=["id"],
        partition_by="date",
        partition_granularity="month",
    ))
    assert req.sync_strategy == "partitioned"
    assert req.partition_by == "date"


def test_unknown_strategy_rejected():
    with pytest.raises(ValidationError, match="sync_strategy"):
        RegisterTableRequest(**_base(sync_strategy="weekly_at_midnight"))


# ───────────────────────────── partition requirements ─────────────────────────


def test_partitioned_without_partition_by_rejected():
    with pytest.raises(ValidationError, match="partition_by"):
        RegisterTableRequest(**_base(sync_strategy="partitioned"))


def test_partitioned_invalid_granularity_rejected():
    with pytest.raises(ValidationError, match="partition_granularity"):
        RegisterTableRequest(**_base(
            sync_strategy="partitioned",
            partition_by="date",
            partition_granularity="hour",
        ))


def test_partitioned_with_remote_query_mode_rejected():
    """partitioned + query_mode='remote' is incompatible — partitioned writes
    parquets locally, remote means no local materialization."""
    with pytest.raises(ValidationError, match="partitioned.*remote"):
        RegisterTableRequest(**_base(
            sync_strategy="partitioned",
            partition_by="date",
            partition_granularity="month",
            query_mode="remote",
        ))


def test_partitioned_default_granularity_is_month():
    """When partition_granularity omitted, default to 'month' per legacy."""
    req = RegisterTableRequest(**_base(
        sync_strategy="partitioned",
        partition_by="date",
    ))
    assert req.partition_granularity == "month"


# ───────────────────────────── incremental + filters conflict ─────────────────


def test_incremental_with_where_filters_rejected():
    """changedSince already does temporal filtering; layering whereFilters
    on top is conceptually broken (legacy repo silently ignores them).
    Reject loudly."""
    with pytest.raises(ValidationError, match="incremental.*where_filters"):
        RegisterTableRequest(**_base(
            sync_strategy="incremental",
            primary_key=["id"],
            where_filters=[
                {"column": "country", "operator": "eq", "values": ["CZ"]},
            ],
        ))


def test_full_refresh_with_where_filters_accepted():
    req = RegisterTableRequest(**_base(
        sync_strategy="full_refresh",
        where_filters=[
            {"column": "date", "operator": "ge", "values": ["{{last_3_months}}"]},
        ],
    ))
    assert req.where_filters is not None
    assert req.where_filters[0]["column"] == "date"


def test_partitioned_with_where_filters_accepted():
    req = RegisterTableRequest(**_base(
        sync_strategy="partitioned",
        partition_by="date",
        where_filters=[
            {"column": "date", "operator": "ge", "values": ["{{last_year}}"]},
        ],
    ))
    assert req.where_filters[0]["values"] == ["{{last_year}}"]


# ───────────────────────────── where_filters shape validation ─────────────────


def test_invalid_where_filter_operator_rejected():
    with pytest.raises(ValidationError, match="operator"):
        RegisterTableRequest(**_base(
            where_filters=[
                {"column": "x", "operator": "like", "values": ["%foo"]},
            ],
        ))


def test_where_filter_missing_values_rejected():
    with pytest.raises(ValidationError, match="values"):
        RegisterTableRequest(**_base(
            where_filters=[
                {"column": "x", "operator": "eq", "values": []},
            ],
        ))


def test_where_filter_unknown_placeholder_accepted_at_register():
    """Placeholders are resolved at SYNC time, not registration. A typo'd
    placeholder must register OK and only fail when the next sync runs."""
    req = RegisterTableRequest(**_base(
        where_filters=[
            {"column": "date", "operator": "ge", "values": ["{{lasst_week}}"]},
        ],
    ))
    assert req.where_filters[0]["values"] == ["{{lasst_week}}"]
