"""Tests for where_filter parse + placeholder resolution."""
from datetime import datetime, timezone

import pytest


NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


# ───────────────────────────── resolve_placeholders ───────────────────────────


def test_resolve_today():
    from connectors.keboola.where_filters import resolve_placeholders
    filters = [{"column": "date", "operator": "ge", "values": ["{{today}}"]}]
    out = resolve_placeholders(filters, NOW)
    assert out == [{"column": "date", "operator": "ge", "values": ["2026-05-07"]}]


def test_resolve_last_week():
    from connectors.keboola.where_filters import resolve_placeholders
    out = resolve_placeholders(
        [{"column": "date", "operator": "ge", "values": ["{{last_week}}"]}], NOW
    )
    assert out[0]["values"] == ["2026-04-30"]


def test_resolve_last_month():
    from connectors.keboola.where_filters import resolve_placeholders
    out = resolve_placeholders(
        [{"column": "date", "operator": "ge", "values": ["{{last_month}}"]}], NOW
    )
    assert out[0]["values"] == ["2026-04-07"]


def test_resolve_last_3_months():
    from connectors.keboola.where_filters import resolve_placeholders
    out = resolve_placeholders(
        [{"column": "date", "operator": "ge", "values": ["{{last_3_months}}"]}], NOW
    )
    assert out[0]["values"] == ["2026-02-06"]


def test_resolve_start_of_3_months_ago():
    """First day of the calendar month 3 months prior. NOW=2026-05-07 → 2026-02-01."""
    from connectors.keboola.where_filters import resolve_placeholders
    out = resolve_placeholders(
        [{"column": "date", "operator": "ge", "values": ["{{start_of_3_months_ago}}"]}], NOW
    )
    assert out[0]["values"] == ["2026-02-01"]


def test_literal_values_pass_through_unchanged():
    from connectors.keboola.where_filters import resolve_placeholders
    filters = [{"column": "country", "operator": "eq", "values": ["CZ", "SK"]}]
    out = resolve_placeholders(filters, NOW)
    assert out == filters


def test_mixed_literal_and_placeholder_values():
    from connectors.keboola.where_filters import resolve_placeholders
    out = resolve_placeholders(
        [{"column": "type", "operator": "eq", "values": ["paid", "{{today}}"]}], NOW
    )
    assert out[0]["values"] == ["paid", "2026-05-07"]


def test_unknown_placeholder_raises():
    from connectors.keboola.where_filters import resolve_placeholders, InvalidFilterError
    filters = [{"column": "date", "operator": "ge", "values": ["{{tomorrow}}"]}]
    with pytest.raises(InvalidFilterError, match="tomorrow"):
        resolve_placeholders(filters, NOW)


def test_supported_placeholders_complete():
    """The advertised set must match what `_placeholder_value` handles."""
    from connectors.keboola.where_filters import SUPPORTED_PLACEHOLDERS
    assert SUPPORTED_PLACEHOLDERS == frozenset({
        "{{today}}", "{{last_week}}", "{{last_month}}", "{{last_2_months}}",
        "{{last_3_months}}", "{{last_6_months}}", "{{last_year}}",
        "{{last_2_years}}", "{{start_of_3_months_ago}}",
    })


def test_resolve_year_month_boundary():
    """start_of_3_months_ago must wrap year boundaries correctly."""
    from connectors.keboola.where_filters import resolve_placeholders
    feb = datetime(2026, 2, 15, tzinfo=timezone.utc)  # 3 months back = 2025-11
    out = resolve_placeholders(
        [{"column": "date", "operator": "ge", "values": ["{{start_of_3_months_ago}}"]}], feb
    )
    assert out[0]["values"] == ["2025-11-01"]


# ───────────────────────────── parse_filters ──────────────────────────────────


def test_parse_from_json_string():
    from connectors.keboola.where_filters import parse_filters
    out = parse_filters('[{"column": "date", "operator": "ge", "values": ["2026-01-01"]}]')
    assert out == [{"column": "date", "operator": "ge", "values": ["2026-01-01"]}]


def test_parse_from_list_passthrough():
    from connectors.keboola.where_filters import parse_filters
    val = [{"column": "x", "operator": "eq", "values": ["a"]}]
    assert parse_filters(val) == val


def test_parse_empty_returns_empty_list():
    from connectors.keboola.where_filters import parse_filters
    assert parse_filters("") == []
    assert parse_filters(None) == []
    assert parse_filters([]) == []


def test_parse_invalid_json_raises():
    from connectors.keboola.where_filters import parse_filters, InvalidFilterError
    with pytest.raises(InvalidFilterError, match="JSON"):
        parse_filters("not-json")


def test_parse_not_an_array_raises():
    from connectors.keboola.where_filters import parse_filters, InvalidFilterError
    with pytest.raises(InvalidFilterError, match="array"):
        parse_filters('{"column": "x"}')


def test_parse_missing_column_raises():
    from connectors.keboola.where_filters import parse_filters, InvalidFilterError
    with pytest.raises(InvalidFilterError, match="column"):
        parse_filters('[{"operator": "eq", "values": ["x"]}]')


def test_parse_invalid_operator_raises():
    from connectors.keboola.where_filters import parse_filters, InvalidFilterError
    with pytest.raises(InvalidFilterError, match="operator"):
        parse_filters('[{"column": "x", "operator": "like", "values": ["%foo"]}]')


def test_parse_empty_values_raises():
    from connectors.keboola.where_filters import parse_filters, InvalidFilterError
    with pytest.raises(InvalidFilterError, match="values"):
        parse_filters('[{"column": "x", "operator": "eq", "values": []}]')


def test_parse_non_string_value_coerced_to_string():
    """Keboola Storage API expects strings on the wire, even for numeric values."""
    from connectors.keboola.where_filters import parse_filters
    out = parse_filters('[{"column": "id", "operator": "gt", "values": [100]}]')
    assert out == [{"column": "id", "operator": "gt", "values": ["100"]}]


def test_parse_default_operator_is_eq():
    """When operator is omitted, default to 'eq' (Keboola Storage API default)."""
    from connectors.keboola.where_filters import parse_filters
    out = parse_filters('[{"column": "x", "values": ["a"]}]')
    assert out[0]["operator"] == "eq"
