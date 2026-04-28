"""WHERE clause validator for /api/v2/scan.

Single security perimeter — every analyst-supplied predicate flows through here
before reaching BigQuery. Allow-list-driven; explicit rejection codes per spec §3.7.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Mapping

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

logger = logging.getLogger(__name__)

# Rejection kind codes (stable; used by callers + tests + audit log)
REJECT_PARSE = "parse_error"
REJECT_NESTED_SELECT = "nested_select"
REJECT_MULTI_STATEMENT = "multi_statement"
REJECT_DDL_DML = "ddl_or_dml"
REJECT_CROSS_TABLE = "cross_table_reference"
REJECT_UNKNOWN_FUNCTION = "unknown_function"
REJECT_UNKNOWN_COLUMN = "unknown_column"
REJECT_DISALLOWED_NODE = "disallowed_node"


@dataclass
class WhereValidationError(Exception):
    kind: str
    message: str
    detail: dict | None = None

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


# Nodes that imply DDL/DML (rejected outright).
_DDL_DML_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.TruncateTable,
    exp.Alter, exp.Create, exp.Copy, exp.Merge,
)


# v1 BigQuery function allow-list (spec §3.7). Stored as upper-case names.
_ALLOW_FUNCTIONS_DATETIME = {
    "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME",
    "DATE", "DATETIME", "TIMESTAMP", "TIME",
    "DATE_ADD", "DATE_SUB", "DATE_DIFF", "DATE_TRUNC", "EXTRACT",
    "FORMAT_DATE", "FORMAT_TIMESTAMP", "PARSE_DATE", "PARSE_TIMESTAMP",
    "UNIX_SECONDS", "UNIX_MILLIS",
}
_ALLOW_FUNCTIONS_STRING = {
    "CONCAT", "LENGTH", "LOWER", "UPPER", "SUBSTR", "SUBSTRING",
    "TRIM", "LTRIM", "RTRIM", "REPLACE",
    "STARTS_WITH", "ENDS_WITH", "CONTAINS_SUBSTR",
    "REGEXP_CONTAINS", "REGEXP_EXTRACT", "SAFE_CAST",
    # sqlglot normalizes some BQ funcs to a canonical SQL name; allow both spellings.
    "REGEXP_LIKE",  # sqlglot canonical for REGEXP_CONTAINS
}
_ALLOW_FUNCTIONS_MATH = {
    "ABS", "CEIL", "FLOOR", "ROUND", "MOD", "POWER", "SQRT",
    "LOG", "LN", "EXP", "SIGN", "GREATEST", "LEAST",
}
_ALLOW_FUNCTIONS_CAST = {"CAST"}
_ALLOW_FUNCTIONS_CONDITIONAL = {"IF", "IFNULL", "COALESCE", "NULLIF", "CASE"}

ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    _ALLOW_FUNCTIONS_DATETIME
    | _ALLOW_FUNCTIONS_STRING
    | _ALLOW_FUNCTIONS_MATH
    | _ALLOW_FUNCTIONS_CAST
    | _ALLOW_FUNCTIONS_CONDITIONAL
)


def validate_where(
    predicate: str,
    table_id: str,
    schema: Mapping[str, str],
    *,
    dialect: str = "bigquery",
) -> exp.Expression:
    """Validate a WHERE-clause fragment.

    Args:
        predicate: SQL fragment (without leading 'WHERE').
        table_id: target table id; cross-table references rejected.
        schema: {column_name: type} for the target table.
        dialect: sqlglot dialect to parse with. Default 'bigquery'. Pass 'duckdb'
            (or anything sqlglot supports) when the predicate will be executed
            against a local DuckDB scan, so DuckDB-specific syntax parses.

    Returns:
        Parsed sqlglot expression tree (caller may re-stringify or inspect).

    Raises:
        WhereValidationError: with .kind set to one of the REJECT_* codes.
    """
    if not predicate or not predicate.strip():
        raise WhereValidationError(REJECT_PARSE, "empty predicate")

    # Multi-statement detection: BQ statements separated by ';' would parse
    # as multiple expressions in sqlglot.parse() (returns a list).
    try:
        statements = sqlglot.parse(f"SELECT 1 FROM t WHERE {predicate}", dialect=dialect)
    except ParseError as e:
        raise WhereValidationError(REJECT_PARSE, f"parse failed: {e}")

    if statements is None or len(statements) != 1 or statements[0] is None:
        raise WhereValidationError(REJECT_MULTI_STATEMENT, "multi-statement input not allowed")

    select = statements[0]
    # A predicate like `1=1 UNION ALL SELECT secret FROM x` parses as a single
    # `exp.Union` (not `exp.Select`), and `find(exp.Where)` would return only
    # the left side's `1=1` — passing structural checks while the raw predicate
    # string still gets concatenated into the final SQL. Reject here.
    if not isinstance(select, exp.Select):
        raise WhereValidationError(
            REJECT_DISALLOWED_NODE,
            f"top-level statement must be SELECT, got {type(select).__name__}",
        )
    where = select.find(exp.Where)
    if where is None:
        raise WhereValidationError(REJECT_PARSE, "no WHERE expression found in parsed input")

    _walk_structural(where, table_id, schema)
    return where


def _walk_structural(node: exp.Expression, table_id: str, schema: Mapping[str, str]) -> None:
    """Walk the WHERE AST and reject disallowed structures."""
    for sub in node.walk():
        # `node.walk()` yields the node itself first; check structural rules.
        if isinstance(sub, exp.Subquery) or (isinstance(sub, exp.Select) and sub is not node):
            raise WhereValidationError(REJECT_NESTED_SELECT, "nested SELECT/subquery not allowed")
        if isinstance(sub, _DDL_DML_NODES):
            raise WhereValidationError(REJECT_DDL_DML, f"DDL/DML node {type(sub).__name__} not allowed")

    # Cross-table reference detection: any column with a qualifier other than
    # the target table_id (or unqualified) is rejected.
    for col in node.find_all(exp.Column):
        qualifier = col.table  # e.g. "other_table" in `other_table.id`
        if qualifier and qualifier.lower() != table_id.lower():
            raise WhereValidationError(
                REJECT_CROSS_TABLE,
                f"column {col.sql()} references table {qualifier!r}, expected {table_id!r}",
            )

    _walk_functions(node)
    _walk_columns(node, schema)


def _walk_columns(node: exp.Expression, schema: Mapping[str, str]) -> None:
    """Reject column references not present in the target table's schema."""
    known = {c.lower() for c in schema}
    for col in node.find_all(exp.Column):
        # `col.name` is the leaf column name (e.g. "country_code" in
        # "tbl.country_code"). For dotted struct fields like "rec.sub.leaf",
        # sqlglot models as nested exp.Dot; v1 only checks top-level names.
        leaf = (col.name or "").lower()
        if leaf and leaf not in known:
            raise WhereValidationError(
                REJECT_UNKNOWN_COLUMN,
                f"column {col.name!r} not in schema for {col.table!r}",
                detail={"column": col.name},
            )


def _walk_functions(node: exp.Expression) -> None:
    """Reject function calls outside the allow-list.

    sqlglot represents function calls in two ways:
      - typed subclasses (e.g. ``exp.Length``, ``exp.StartsWith``, ``exp.SessionUser``,
        ``exp.Cast``, ``exp.Coalesce``) — canonical SQL name available via ``sql_name()``;
      - ``exp.Anonymous`` for unknown built-ins or UDFs — name in ``func.name``.
    Both paths funnel into ``ALLOWED_FUNCTIONS``; everything else is rejected.
    """
    for func in node.find_all(exp.Func):
        # Logical connectors (AND/OR/XOR) inherit exp.Func in sqlglot but are
        # operators, not user-callable functions. Skip them.
        if isinstance(func, exp.Connector):
            continue

        if isinstance(func, exp.AggFunc):
            raise WhereValidationError(
                REJECT_UNKNOWN_FUNCTION,
                f"aggregate function not allowed in WHERE: {type(func).__name__}",
                detail={"function": type(func).__name__.upper()},
            )

        # `Anonymous` carries the source name in `func.name`; typed nodes carry
        # their canonical SQL name via `sql_name()`. `name` on typed nodes often
        # holds the first child's identifier, so we never trust it directly.
        if isinstance(func, exp.Anonymous):
            name = (func.name or "").upper()
        else:
            try:
                name = (func.sql_name() or "").upper()
            except Exception:
                name = ""

        # If sql_name() returns empty for a typed Func, we can't tell whether
        # it's a benign operator wrapper or a future dangerous construct.
        # Reject (defense in depth) — if a legitimate case appears, add the
        # specific subclass to the explicit-skip list above (Connector, etc.).
        if not name:
            raise WhereValidationError(
                REJECT_UNKNOWN_FUNCTION,
                f"unrecognized function-like node: {type(func).__name__}",
                detail={"function": type(func).__name__},
            )

        if name not in ALLOWED_FUNCTIONS:
            raise WhereValidationError(
                REJECT_UNKNOWN_FUNCTION,
                f"function not in v1 allow-list: {name}",
                detail={"function": name},
            )
