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


def validate_where(
    predicate: str,
    table_id: str,
    schema: Mapping[str, str],
) -> exp.Expression:
    """Validate a WHERE-clause fragment.

    Args:
        predicate: SQL fragment (without leading 'WHERE').
        table_id: target table id; cross-table references rejected.
        schema: {column_name: type} for the target table.

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
        statements = sqlglot.parse(f"SELECT 1 FROM t WHERE {predicate}", dialect="bigquery")
    except ParseError as e:
        raise WhereValidationError(REJECT_PARSE, f"parse failed: {e}")

    if statements is None or len(statements) != 1 or statements[0] is None:
        raise WhereValidationError(REJECT_MULTI_STATEMENT, "multi-statement input not allowed")

    select = statements[0]
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
