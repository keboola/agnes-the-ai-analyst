"""Compile a structured builder spec into the canonical DuckDB policy SQL.

The stored policy is always SQL -- ``src/access_policy.py`` runs the body
verbatim -- so this module is the no-SQL builder's *generator*, not a second
source of truth. Its one hard invariant: a re-derived column (any mask that
emits ``<expr> AS col``) is ALWAYS added to ``* EXCLUDE (...)`` first, so the
two-column plaintext leak ``SELECT *, md5(col) AS col`` can never be produced
(the failure class caught after-the-fact by ``policy_duplicate_output_column``
today). Pure and HTTP-free so it unit-tests without a request and can be reused
by the CLI later.

The spec shape (all keys optional except ``table``)::

    {
      "table": "invoices",
      "row_rules": [{"column": str, "op": ROW_OP, "value": Any}],
      "row_combine": "and" | "or",
      "column_masks": {col: MASK | {"choice": MASK, "group": str}},
    }

``ROW_OP`` is one of ``in_caller_groups`` (row's column is one of the caller's
live groups), ``eq_caller_email`` / ``eq_caller_id`` (self-owned rows), ``eq``
/ ``in`` (literal match). ``MASK`` is ``show`` | ``hide`` | ``nullify`` |
``hash`` | ``unmask`` (``unmask`` needs a ``group``). Unknown columns are
dropped with a warning rather than reaching the SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.sql_ident import quote_ident

# Row operators the builder can emit. Kept explicit so an unknown op is a loud
# error, not a silently-dropped rule.
_ID_TOKENS = {
    "eq_caller_email": "$user_email",
    "eq_caller_id": "$user_id",
}


@dataclass
class CompiledPolicy:
    sql: str
    excluded: list[str] = field(default_factory=list)
    derived: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _mask_choice(m: Any) -> str:
    return m.get("choice") if isinstance(m, dict) else m


def _sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):  # bool is an int subclass -- guard before the numeric branch
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


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


def compile_policy(spec: dict, columns: list[str]) -> CompiledPolicy:
    """Turn a structured builder ``spec`` into canonical policy SQL.

    ``columns`` is the table's real column list (from a DESCRIBE); anything the
    spec references that is not in it is dropped with a warning, so a stale
    builder state can never smuggle an unknown identifier into the SQL.
    """
    known = set(columns)
    warnings: list[str] = []
    excluded: list[str] = []
    derived: list[str] = []

    for col, raw in (spec.get("column_masks") or {}).items():
        if col not in known:
            warnings.append(f"unknown column ignored: {col}")
            continue
        choice = _mask_choice(raw)
        if choice == "show":
            continue
        # THE anti-leak invariant: every non-show mask excludes the column from
        # `*` before anything re-derives it. `hide` stops here; the rest append
        # a replacement projection below.
        excluded.append(col)
        q = quote_ident(col)
        if choice == "hide":
            continue
        if choice == "nullify":
            derived.append(f"NULL AS {q}")
        elif choice == "hash":
            derived.append(f"md5({q}) AS {q}")
        elif choice == "unmask":
            grp = raw.get("group", "") if isinstance(raw, dict) else ""
            derived.append(f"CASE WHEN list_contains($user_groups, {_sql_literal(grp)}) THEN {q} ELSE NULL END AS {q}")
        else:
            raise ValueError(f"unknown mask: {choice!r}")

    proj = "*"
    if excluded:
        proj = "* EXCLUDE (" + ", ".join(quote_ident(c) for c in excluded) + ")"
    if derived:
        proj = proj + ", " + ", ".join(derived)

    rules = [r for r in (spec.get("row_rules") or []) if r.get("column") in known]
    dropped_rules = [r for r in (spec.get("row_rules") or []) if r.get("column") not in known]
    for r in dropped_rules:
        warnings.append(f"unknown column ignored: {r.get('column')}")

    where = ""
    if rules:
        joiner = " OR " if spec.get("row_combine") == "or" else " AND "
        where = " WHERE " + joiner.join(_predicate(r) for r in rules)

    sql = f"SELECT {proj} FROM {quote_ident(spec['table'])}{where}"
    if not rules and not excluded and not derived:
        warnings.append("This policy returns the full table to every caller -- nothing is filtered or masked.")
    return CompiledPolicy(sql=sql, excluded=excluded, derived=derived, warnings=warnings)
