"""Compile a structured builder spec into the canonical DuckDB policy SQL.

The stored policy is always SQL -- ``src/access_policy.py`` runs the body
verbatim -- so this module is the no-SQL builder's *generator*, not a second
source of truth. Its hard invariants:

1. The projection is **explicit** and **fixed at save time**; no ``SELECT *``
   survives. A new source column added after the policy is saved is therefore
   never returned (deny-by-omission), and a removed source column causes the
   policy to fail closed at execution time (deny-by-error).
2. A masked column is listed exactly once in the projection, so the
   two-column plaintext leak ``SELECT *, md5(col) AS col`` can never be
   produced.
3. ``unmask`` masks preserve the original column type for allowed groups and
   return ``'*****'`` for text-like columns / ``NULL`` for all other types when
   the caller is not in any allowed group.

Pure and HTTP-free so it unit-tests without a request and can be reused by the
CLI later.

The spec shape (all keys optional except ``table``)::

    {
      "table": "invoices",
      "row_rules": [{"column": str, "op": ROW_OP, "value": Any}],
      "row_combine": "and" | "or",
      "column_masks": {col: MASK | {"choice": MASK, "group": str} |
                              {"choice": MASK, "groups": [str, ...]}},
    }

``ROW_OP`` is one of ``in_caller_groups`` (row's column is one of the caller's
live groups), ``eq_caller_email`` / ``eq_caller_id`` (self-owned rows), ``eq``
/ ``in`` (literal match). ``MASK`` is ``show`` | ``hide`` | ``nullify`` |
``hash`` | ``unmask`` (``unmask`` needs a ``group`` or ``groups`` list). Unknown
columns are dropped with a warning rather than reaching the SQL.

``columns`` is the table's real column list from a DESCRIBE; each entry may be
a column name string, a ``(name, type)`` tuple, or a ``{"name": ..., "type": ...}``
dict. Anything the spec references that is not in the list is dropped with a
warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from src.sql_ident import quote_ident

# Row operators the builder can emit. Kept explicit so an unknown op is a loud
# error, not a silently-dropped rule.
_ID_TOKENS = {
    "eq_caller_email": "$user_email",
    "eq_caller_id": "$user_id",
}

# DuckDB types that should be treated as text for the unmask fallback.
_TEXT_TYPE_KEYWORDS = ("VARCHAR", "TEXT", "STRING")


@dataclass
class CompiledPolicy:
    sql: str
    excluded: list[str] = field(default_factory=list)
    derived: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _mask_choice(m: Any) -> str:
    if isinstance(m, dict):
        return str(m.get("choice", ""))
    return str(m)


def _sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):  # bool is an int subclass -- guard before the numeric branch
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _normalize_columns(columns: Sequence[Any]) -> list[tuple[str, str]]:
    """Convert flexible column descriptors into (name, type) pairs.

    Strings are accepted for backwards compatibility in unit tests, defaulting
    type to VARCHAR. Callers that have real DESCRIBE output should always pass
    (name, type) tuples or dicts.
    """
    out: list[tuple[str, str]] = []
    for c in columns:
        if isinstance(c, str):
            out.append((c, "VARCHAR"))
        elif isinstance(c, dict):
            out.append((str(c["name"]), str(c.get("type", "VARCHAR"))))
        elif isinstance(c, (list, tuple)) and len(c) >= 2:
            out.append((str(c[0]), str(c[1])))
        else:
            raise ValueError(f"unsupported column descriptor: {c!r}")
    return out


def _is_text_type(col_type: str) -> bool:
    if not col_type:
        return False
    upper = col_type.upper()
    return any(keyword in upper for keyword in _TEXT_TYPE_KEYWORDS)


def _unmask_condition(groups: list[str]) -> str:
    """Build the WHEN clause for an unmask mask.

    Empty allowlist means no one is allowed to see the real value, so the
    condition is ``FALSE``.
    """
    if not groups:
        return "FALSE"
    tests = [f"list_contains($user_groups, {_sql_literal(g)})" for g in groups]
    return " OR ".join(tests) if len(tests) > 1 else tests[0]


def _unmask_groups(raw: Any) -> list[str]:
    """Extract the allowlist group(s) from an unmask mask entry."""
    if isinstance(raw, dict):
        if "groups" in raw:
            groups = raw["groups"]
            return [g for g in (groups if isinstance(groups, (list, tuple)) else [groups]) if g]
        if raw.get("group"):
            return [raw["group"]]
    return []


def _masked_fallback(col_type: str) -> str:
    """The fallback value for an unmask mask when the caller is not allowed.

    Text-like columns get a fixed redaction string; everything else gets a
    type-preserving NULL so the CASE expression keeps the original column type.
    """
    if _is_text_type(col_type):
        return _sql_literal("*****")
    return f"CAST(NULL AS {col_type})"


def _predicate(rule: dict) -> str:
    col = quote_ident(rule["column"])
    op = rule.get("op")
    if op == "in_caller_groups":
        # The transpile-safe idiom the design doc mandates (never `col IN
        # (unnest($user_groups))`, which the validator only warns on and
        # BigQuery bloats).
        return f"list_contains($user_groups, {col})"
    if op in _ID_TOKENS:
        return f"{col} = {_ID_TOKENS[op]}"
    if op == "eq":
        return f"{col} = {_sql_literal(rule.get('value'))}"
    if op == "in":
        vals = rule.get("value") or []
        return f"{col} IN (" + ", ".join(_sql_literal(v) for v in vals) + ")"
    raise ValueError(f"unknown row op: {op!r}")


def compile_policy(spec: dict, columns: Sequence[Any]) -> CompiledPolicy:
    """Turn a structured builder ``spec`` into canonical policy SQL."""
    col_info = _normalize_columns(columns)
    known = {c[0] for c in col_info}
    col_type_by_name = {c[0]: c[1] for c in col_info}
    warnings: list[str] = []
    excluded: list[str] = []
    derived: list[str] = []
    projections: list[str] = []

    # Build an expression for each masked column, then assemble the final
    # projection in the table's native column order. This preserves the output
    # schema order, avoids duplicate projections, and never falls back to `*`.
    masked_exprs: dict[str, str] = {}
    for col, raw in (spec.get("column_masks") or {}).items():
        if col not in known:
            warnings.append(f"unknown column ignored: {col}")
            continue
        choice = _mask_choice(raw)
        q = quote_ident(col)
        col_type = col_type_by_name.get(col, "VARCHAR") or "VARCHAR"
        if choice == "show":
            continue
        excluded.append(col)
        if choice == "hide":
            # Hidden columns are omitted from the fixed projection entirely.
            continue
        if choice == "nullify":
            # Cast keeps the column type unchanged for the caller.
            expr = f"CAST(NULL AS {col_type}) AS {q}"
            derived.append(col)
        elif choice == "hash":
            expr = f"md5({q}) AS {q}"
            derived.append(col)
        elif choice == "unmask":
            groups = _unmask_groups(raw)
            fallback = _masked_fallback(col_type)
            expr = f"CASE WHEN {_unmask_condition(groups)} THEN {q} ELSE {fallback} END AS {q}"
            derived.append(col)
        else:
            raise ValueError(f"unknown mask: {choice!r}")
        masked_exprs[col] = expr

    projections = []
    for name, _ in col_info:
        if name in masked_exprs:
            projections.append(masked_exprs[name])
        elif name not in excluded:
            projections.append(quote_ident(name))

    if not projections:
        # Fail closed: a policy that would project nothing cannot become `SELECT *`.
        raise ValueError("policy would select no columns; leave at least one column visible")

    rules = [r for r in (spec.get("row_rules") or []) if r.get("column") in known]
    dropped_rules = [r for r in (spec.get("row_rules") or []) if r.get("column") not in known]
    for r in dropped_rules:
        warnings.append(f"unknown column ignored: {r.get('column')}")

    where = ""
    if rules:
        joiner = " OR " if spec.get("row_combine") == "or" else " AND "
        where = " WHERE " + joiner.join(_predicate(r) for r in rules)

    sql = f"SELECT {', '.join(projections)} FROM {quote_ident(spec['table'])}{where}"
    if not rules and not excluded and not derived:
        warnings.append("This policy returns the full table to every caller -- nothing is filtered or masked.")
    return CompiledPolicy(sql=sql, excluded=excluded, derived=derived, warnings=warnings)
