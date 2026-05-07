"""Keboola whereFilters: parse, validate, and resolve date placeholders.

Mirrors internal repo's `src/config.py:_resolve_placeholder` (lines 269-317),
but uses a ports-and-adapters shape: pure functions, no dataclass coupling,
no global `datetime.now()` (caller injects).

The Keboola Storage API understands `whereFilters` natively — each entry
is `{column, operator, values}` where `operator ∈ {eq,ne,gt,ge,lt,le}`
and `values` is a list. Multiple filter entries are AND'd. Multiple
items in one entry's `values` list are IN'd (operator='eq') or
NOT-IN'd (operator='ne'). There is no OR support and no nested boolean
logic — match the API's vocabulary.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List


SUPPORTED_OPERATORS = frozenset({"eq", "ne", "gt", "ge", "lt", "le"})
SUPPORTED_PLACEHOLDERS = frozenset({
    "{{today}}",
    "{{last_week}}",
    "{{last_month}}",
    "{{last_2_months}}",
    "{{last_3_months}}",
    "{{last_6_months}}",
    "{{last_year}}",
    "{{last_2_years}}",
    "{{start_of_3_months_ago}}",
})


class InvalidFilterError(ValueError):
    """Filter shape, operator, or placeholder is invalid."""


# ───────────────────────────── placeholder resolution ─────────────────────────


def resolve_placeholders(
    filters: List[Dict[str, Any]],
    now: datetime,
) -> List[Dict[str, Any]]:
    """Walk every filter's `values` list and substitute date placeholders.

    Unknown placeholders raise `InvalidFilterError` — silent passthrough
    would be a footgun (admin types `{{lasst_week}}` and the extractor
    sends a literal string to the Storage API, which compares it
    verbatim and returns 0 rows).
    """
    return [
        {**f, "values": [_resolve_one(v, now) for v in f.get("values", [])]}
        for f in filters
    ]


def _resolve_one(value: Any, now: datetime) -> Any:
    if not isinstance(value, str):
        return value
    if "{{" not in value:
        return value
    out = value
    for token in SUPPORTED_PLACEHOLDERS:
        if token in out:
            out = out.replace(token, _placeholder_value(token, now))
    if "{{" in out:
        # Surface the offending token specifically so the operator sees what to fix
        start = out.index("{{")
        end = out.index("}}", start) + 2 if "}}" in out[start:] else len(out)
        unknown = out[start:end]
        raise InvalidFilterError(f"Unknown placeholder: {unknown}")
    return out


def _placeholder_value(token: str, now: datetime) -> str:
    fmt = "%Y-%m-%d"
    today = now.date()
    if token == "{{today}}":
        return today.strftime(fmt)
    if token == "{{last_week}}":
        return (today - timedelta(days=7)).strftime(fmt)
    if token == "{{last_month}}":
        return (today - timedelta(days=30)).strftime(fmt)
    if token == "{{last_2_months}}":
        return (today - timedelta(days=60)).strftime(fmt)
    if token == "{{last_3_months}}":
        return (today - timedelta(days=90)).strftime(fmt)
    if token == "{{last_6_months}}":
        return (today - timedelta(days=180)).strftime(fmt)
    if token == "{{last_year}}":
        return (today - timedelta(days=365)).strftime(fmt)
    if token == "{{last_2_years}}":
        return (today - timedelta(days=730)).strftime(fmt)
    if token == "{{start_of_3_months_ago}}":
        # First day of the calendar month 3 months prior
        y, m = today.year, today.month
        m -= 3
        if m <= 0:
            m += 12
            y -= 1
        return f"{y:04d}-{m:02d}-01"
    raise InvalidFilterError(f"Unhandled placeholder: {token}")


# ───────────────────────────── parse + validate ───────────────────────────────


def parse_filters(raw: Any) -> List[Dict[str, Any]]:
    """Parse and validate a where_filters payload.

    Accepts:
    - None / empty string / empty list → []
    - JSON string of an array → parsed and validated
    - Already-parsed list → validated in place

    Validates: each entry is `{column: str, operator: SUPPORTED_OPERATORS,
    values: non-empty list of stringifiable scalars}`. Coerces numeric
    values to strings (Storage API expects strings on the wire).
    Operator defaults to 'eq' when omitted.
    """
    if raw is None or raw == "" or raw == []:
        return []

    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise InvalidFilterError(f"Invalid JSON: {e}") from e
    else:
        data = raw

    if not isinstance(data, list):
        raise InvalidFilterError(f"Expected JSON array of filters, got {type(data).__name__}")

    result: List[Dict[str, Any]] = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise InvalidFilterError(f"Filter[{i}] must be an object, got {type(entry).__name__}")

        column = entry.get("column")
        if not isinstance(column, str) or not column:
            raise InvalidFilterError(f"Filter[{i}].column must be a non-empty string")

        operator = entry.get("operator", "eq")
        if operator not in SUPPORTED_OPERATORS:
            raise InvalidFilterError(
                f"Filter[{i}].operator must be one of {sorted(SUPPORTED_OPERATORS)}, got {operator!r}"
            )

        values = entry.get("values")
        if not isinstance(values, list) or not values:
            raise InvalidFilterError(f"Filter[{i}].values must be a non-empty list")

        coerced_values = [str(v) for v in values]

        result.append({"column": column, "operator": operator, "values": coerced_values})

    return result
