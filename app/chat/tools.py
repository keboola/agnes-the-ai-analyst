"""Tool handlers exposed to the chat agent.

Each tool is a thin wrapper over an existing Agnes capability:

  - ``list_catalog``       → table_registry filtered by per-user RBAC
  - ``get_schema``         → DuckDB DESCRIBE against the analytics view
  - ``describe_table``     → SELECT ... LIMIT N sample rows
  - ``run_query``          → local-mode SELECT against analytics.duckdb
  - ``lookup_metric``      → metric_definitions row
  - ``get_memory_bundle``  → Corporate Memory bundle for the caller

Every handler validates RBAC against the caller's identity before
touching data. ``run_query`` parses the submitted SQL for table refs
and refuses execution if any referenced table is ``query_mode='remote'``
or outside the caller's access — chat v1 is read-only and local-only by
design (#459).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import duckdb

from src.db import get_analytics_db_readonly
from src.rbac import can_access_table, get_accessible_tables
from src.repositories.table_registry import TableRegistryRepository
from src.repositories.metrics import MetricRepository

logger = logging.getLogger(__name__)


# Anthropic tool definitions ---------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_catalog",
        "description": (
            "List every Agnes table the caller has access to. Use this "
            "first when the user asks about 'what data we have' or before "
            "composing a query. Returns id, name, description, source_type, "
            "and query_mode for each table."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_schema",
        "description": (
            "Return the column names and types of a single table. Use this "
            "before composing any SELECT so you reference real columns. "
            "RBAC-checked: returns an error for tables the caller cannot read."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_id": {"type": "string"},
            },
            "required": ["table_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "describe_table",
        "description": (
            "Return a small sample of rows from a table so you can see the "
            "real shape of the data (string casing, units, null patterns). "
            "Use this after get_schema and before writing aggregates. "
            "Max 20 rows; default 5."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_id": {"type": "string"},
                "n": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["table_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_query",
        "description": (
            "Execute a read-only SELECT against the Agnes analytics database. "
            "Local-mode and materialized tables only — remote (BigQuery) "
            "tables are NOT supported in chat v1 and will return an error. "
            "RBAC is re-checked for every table referenced. Result is "
            "capped at 1000 rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single SELECT statement."},
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lookup_metric",
        "description": (
            "Return the canonical definition of a business metric — SQL, "
            "grain, business rules, synonyms. Use this BEFORE composing any "
            "query that touches a named metric (revenue, MRR, active users, "
            "etc.) so you never invent metric math."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric_id": {"type": "string"},
            },
            "required": ["metric_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_memory_bundle",
        "description": (
            "Return the caller's Corporate Memory bundle — admin-approved "
            "facts about tables, metrics, and team conventions. Always call "
            "this once at the start of a conversation; the bundle reflects "
            "audience-filtered organizational knowledge that supersedes "
            "any general assumption you may have."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


# Public dispatcher ------------------------------------------------------------

ToolHandler = Callable[[dict[str, Any], dict, duckdb.DuckDBPyConnection], Awaitable[dict[str, Any]]]


@dataclass
class ToolResult:
    """Result of a tool invocation. ``ok=False`` carries an error message
    the LLM will see and can act on."""
    ok: bool
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        if self.ok:
            return self.data
        return {"error": self.data.get("error", "tool failed")} | self.data


async def dispatch(
    name: str,
    args: dict[str, Any],
    user: dict,
    conn: duckdb.DuckDBPyConnection,
) -> ToolResult:
    """Invoke a named tool. Returns ``ToolResult`` — the loop is responsible
    for turning this into a ``tool_result`` block for the next assistant turn.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolResult(ok=False, data={"error": f"unknown tool: {name}"})
    try:
        result = await handler(args or {}, user, conn)
        return ToolResult(ok=True, data=result)
    except _ToolError as exc:
        return ToolResult(ok=False, data={"error": str(exc)})
    except Exception:
        logger.exception("chat tool %s crashed", name)
        return ToolResult(ok=False, data={"error": f"{name} failed internally"})


class _ToolError(Exception):
    """Tools raise this for user-actionable errors (bad input, RBAC denial,
    etc.). The message is sent back to the LLM verbatim."""


# Individual tool handlers -----------------------------------------------------


async def _list_catalog(
    args: dict[str, Any], user: dict, conn: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    repo = TableRegistryRepository(conn)
    rows = repo.list_all()
    accessible = get_accessible_tables(user, conn)
    if accessible is None:
        visible = rows
    else:
        accessible_set = set(accessible)
        visible = [r for r in rows if r["id"] in accessible_set]

    return {
        "tables": [
            {
                "id": r["id"],
                "name": r.get("name"),
                "description": r.get("description"),
                "source_type": r.get("source_type"),
                "query_mode": r.get("query_mode", "local"),
            }
            for r in visible
        ],
        "count": len(visible),
    }


async def _get_schema(
    args: dict[str, Any], user: dict, conn: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    table_id = _require_str(args, "table_id")
    if not can_access_table(user, table_id, conn):
        raise _ToolError(f"access denied to table '{table_id}'")
    row = TableRegistryRepository(conn).get(table_id)
    if not row:
        raise _ToolError(f"table not registered: '{table_id}'")
    columns = _describe_view_columns(table_id)
    return {
        "table_id": table_id,
        "name": row.get("name"),
        "description": row.get("description"),
        "source_type": row.get("source_type"),
        "query_mode": row.get("query_mode", "local"),
        "columns": columns,
        "partition_col": row.get("partition_col"),
    }


async def _describe_table(
    args: dict[str, Any], user: dict, conn: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    table_id = _require_str(args, "table_id")
    n = int(args.get("n", 5))
    n = max(1, min(n, 20))
    if not can_access_table(user, table_id, conn):
        raise _ToolError(f"access denied to table '{table_id}'")
    row = TableRegistryRepository(conn).get(table_id)
    if not row:
        raise _ToolError(f"table not registered: '{table_id}'")
    if row.get("query_mode") == "remote":
        raise _ToolError(
            f"table '{table_id}' is query_mode='remote' (BigQuery); sampling "
            "remote tables is not supported in chat v1 — ask the analyst to "
            "create a snapshot first (agnes snapshot create)"
        )
    analytics = get_analytics_db_readonly()
    try:
        analytics.execute(f'SELECT * FROM "{table_id}" LIMIT ?', [n])
        columns = [d[0] for d in analytics.description]
        rows = analytics.fetchall()
    finally:
        analytics.close()
    return {
        "table_id": table_id,
        "columns": columns,
        "rows": [_serialize_row(r) for r in rows],
        "row_count": len(rows),
    }


# Identifiers that look like table refs after FROM / JOIN. Accepts bare
# identifiers (orders) and double-quoted ones ("Some Table"). Schema-
# qualified refs (kbc.bucket.tbl) are intentionally rejected here —
# they're either remote-attach views (refuse with a clear message) or
# system-internal (out of scope for chat v1).
_TABLE_REF_PATTERN = re.compile(
    r'\b(?:FROM|JOIN)\s+(?:"([^"]+)"|([a-zA-Z_][\w]*))',
    re.IGNORECASE,
)

# Statements / keywords we refuse outright. Lifted from app/api/query.py's
# blocklist but narrower: chat is exclusively SELECT, no DDL, no I/O.
_BLOCKED_SQL_KEYWORDS = (
    "drop ", "delete ", "insert ", "update ", "alter ", "create ",
    "copy ", "attach ", "detach ", "load ", "install ",
    "export ", "import ", "pragma ", "call ",
    "read_csv", "read_json", "read_parquet", "read_text",
    "write_csv", "write_parquet", "read_blob", "read_ndjson",
    "parquet_scan", "json_scan", "csv_scan",
    "query_table", "iceberg_scan", "delta_scan", "bigquery_query",
    "glob(", "list_files",
    "information_schema", "duckdb_tables", "duckdb_columns",
    "duckdb_databases", "duckdb_settings", "duckdb_functions",
    "duckdb_views", "duckdb_indexes", "duckdb_schemas",
    "pragma_table_info", "pragma_storage_info",
    # Path-traversal & multi-statement.
    "'../", '"../',
    ";",
)

_RUN_QUERY_ROW_LIMIT = 1000


async def _run_query(
    args: dict[str, Any], user: dict, conn: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    sql_raw = _require_str(args, "sql").strip()
    if not sql_raw:
        raise _ToolError("sql is required")
    sql_lower = sql_raw.lower()

    # Must start with SELECT or WITH (CTE).
    if not (sql_lower.startswith("select") or sql_lower.startswith("with ")):
        raise _ToolError("only SELECT statements are allowed in chat v1")

    for kw in _BLOCKED_SQL_KEYWORDS:
        if kw in sql_lower:
            raise _ToolError(f"SQL keyword/function not allowed in chat v1: {kw.strip()!r}")

    referenced = _extract_table_refs(sql_raw)
    if not referenced:
        raise _ToolError(
            "could not identify any table reference in the SQL — chat v1 "
            "requires explicit FROM/JOIN against a registered table id"
        )

    registry = TableRegistryRepository(conn)
    for ref in referenced:
        row = registry.get(ref)
        if not row:
            raise _ToolError(
                f"table '{ref}' is not registered — call list_catalog to "
                "see available tables"
            )
        if row.get("query_mode") == "remote":
            raise _ToolError(
                f"table '{ref}' is query_mode='remote' (BigQuery); chat v1 "
                "only supports local + materialized tables — ask the user "
                "to create a snapshot (agnes snapshot create) and try again"
            )
        if not can_access_table(user, ref, conn):
            raise _ToolError(f"access denied to table '{ref}'")

    analytics = get_analytics_db_readonly()
    try:
        analytics.execute(sql_raw)
        columns = [d[0] for d in analytics.description]
        rows = analytics.fetchmany(_RUN_QUERY_ROW_LIMIT + 1)
    except duckdb.Error as exc:
        raise _ToolError(f"DuckDB rejected the query: {exc}")
    finally:
        analytics.close()

    truncated = len(rows) > _RUN_QUERY_ROW_LIMIT
    if truncated:
        rows = rows[:_RUN_QUERY_ROW_LIMIT]
    return {
        "columns": columns,
        "rows": [_serialize_row(r) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "row_limit": _RUN_QUERY_ROW_LIMIT,
        "tables_referenced": sorted(referenced),
    }


async def _lookup_metric(
    args: dict[str, Any], user: dict, conn: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    metric_id = _require_str(args, "metric_id")
    row = MetricRepository(conn).get(metric_id)
    if not row:
        raise _ToolError(f"metric '{metric_id}' not found in metric_definitions")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "display_name": row.get("display_name"),
        "description": row.get("description"),
        "category": row.get("category"),
        "type": row.get("type"),
        "unit": row.get("unit"),
        "grain": row.get("grain"),
        "table_name": row.get("table_name"),
        "tables": row.get("tables"),
        "expression": row.get("expression"),
        "sql": row.get("sql"),
        "time_column": row.get("time_column"),
        "dimensions": row.get("dimensions"),
        "filters": row.get("filters"),
        "synonyms": row.get("synonyms"),
        "notes": row.get("notes"),
    }


async def _get_memory_bundle(
    args: dict[str, Any], user: dict, conn: duckdb.DuckDBPyConnection,
) -> dict[str, Any]:
    # Reuse the same audience-filtering helpers as /api/memory/bundle so the
    # chat agent and the existing endpoint never diverge.
    from app.api.memory import _effective_groups, _caller_granted_memory_domains
    from src.repositories.knowledge import KnowledgeRepository

    repo = KnowledgeRepository(conn)
    effective_groups = _effective_groups(user, conn)
    granted_domains = _caller_granted_memory_domains(user, conn)
    dismissed_by = user.get("id")

    mandatory = repo.list_items(
        is_required=True,
        exclude_personal=True,
        user_groups=effective_groups,
        granted_domains=granted_domains,
        dismissed_by_user=dismissed_by,
        hide_dismissed=True,
        limit=1000,
        offset=0,
    )
    approved = repo.list_items(
        statuses=["approved"],
        is_required=False,
        exclude_personal=True,
        user_groups=effective_groups,
        granted_domains=granted_domains,
        dismissed_by_user=dismissed_by,
        hide_dismissed=True,
        limit=1000,
        offset=0,
    )

    return {
        "mandatory": [_compact_memory_item(it) for it in mandatory],
        "approved": [_compact_memory_item(it) for it in approved],
        "mandatory_count": len(mandatory),
        "approved_count": len(approved),
    }


_HANDLERS: dict[str, ToolHandler] = {
    "list_catalog":      _list_catalog,
    "get_schema":        _get_schema,
    "describe_table":    _describe_table,
    "run_query":         _run_query,
    "lookup_metric":     _lookup_metric,
    "get_memory_bundle": _get_memory_bundle,
}


# Helpers ----------------------------------------------------------------------


def _require_str(args: dict[str, Any], key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or not val.strip():
        raise _ToolError(f"missing required argument: {key}")
    return val.strip()


def _extract_table_refs(sql: str) -> set[str]:
    refs: set[str] = set()
    for m in _TABLE_REF_PATTERN.finditer(sql):
        ref = m.group(1) or m.group(2)
        if ref:
            refs.add(ref)
    return refs


def _describe_view_columns(table_id: str) -> list[dict[str, str]]:
    analytics = get_analytics_db_readonly()
    try:
        rows = analytics.execute(f'DESCRIBE "{table_id}"').fetchall()
        # DuckDB DESCRIBE returns (column_name, column_type, null, key, default, extra).
        return [{"name": r[0], "type": r[1]} for r in rows]
    finally:
        analytics.close()


def _serialize_row(row: tuple) -> list[Any]:
    out: list[Any] = []
    for v in row:
        if v is None or isinstance(v, (int, float, bool, str)):
            out.append(v)
        else:
            out.append(str(v))
    return out


def _compact_memory_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "content": item.get("content"),
        "category": item.get("category"),
        "confidence": item.get("confidence"),
        "is_required": item.get("is_required"),
    }
