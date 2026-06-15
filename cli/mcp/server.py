"""Agnes MCP server.

Runs as an stdio subprocess started by Claude Desktop.  All tools have
full network access to the Agnes server — unlike the Bash tool sandbox,
which blocks outbound HTTP.

Usage:
    agnes mcp                   # starts the MCP server (stdio transport)

Claude Desktop wires this via .claude/settings.json:
    {
      "mcpServers": {
        "agnes": {
          "command": "/path/to/agnes",
          "args": ["mcp"],
          "type": "stdio"
        }
      }
    }

The setup.py inside the Cowork bundle detects the agnes binary path at
install time and writes the mcpServers block with the correct absolute path.

Credentials are read from ~/.config/agnes/config.yaml (server URL) and
~/.config/agnes/token.json (PAT) — the same files written by setup.py.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from cli.client import api_get
from cli.config import get_server_url, get_token
from cli.lib.pull import run_pull
from cli.v2_client import V2ClientError, api_get_json, api_post_json
from src.duckdb_conn import _open_duckdb

mcp = FastMCP(
    "Agnes",
    instructions=(
        "Agnes is an AI Data Analyst platform. "
        "Use `catalog` first to discover available tables, then `schema` to understand "
        "columns, `describe` for sample rows, and `query` to run SQL. "
        "Run `pull` to sync the latest data before a session."
    ),
)


# ── helpers ────────────────────────────────────────────────────────────────


def _mcp_error(context: str, exc: V2ClientError) -> str:
    """Turn a V2ClientError into a user-readable MCP error string."""
    return f"{context} failed (HTTP {exc.status_code}): {exc}"


# ── tools ──────────────────────────────────────────────────────────────────


@mcp.tool()
def server_info() -> dict:
    """Return the configured Agnes server URL and your account email.

    Useful as a quick connectivity check at the start of a session.
    """
    server_url = get_server_url()
    token = get_token()
    info: dict = {"server_url": server_url, "authenticated": bool(token)}
    try:
        resp = api_get("/api/health")
        if resp.status_code == 200:
            info["health"] = resp.json()
    except Exception:
        info["health"] = "unreachable"
    # Resolve email from /api/me if available
    try:
        me = api_get_json("/api/me")
        info["user_email"] = me.get("email", "")
    except Exception:
        pass
    return info


@mcp.tool()
def catalog() -> dict:
    """List all tables available to you (RBAC-filtered).

    Returns a dict with a ``tables`` list.  Each entry contains:
    - ``id``          — use this in schema / describe / query calls
    - ``name``        — human-readable label
    - ``description`` — what the table contains
    - ``source_type`` — e.g. keboola, bigquery, internal
    - ``query_mode``  — local | remote | materialized | internal
    - ``sql_flavor``  — duckdb or bigquery (affects SQL dialect in query)
    - ``rows``        — approximate row count (may be null)

    Always call this first so you know what data is available.
    """
    try:
        return api_get_json("/api/v2/catalog")
    except V2ClientError as exc:
        raise ValueError(_mcp_error("catalog", exc)) from exc


@mcp.tool()
def collections_list() -> dict:
    """List the file Collections you can access (RBAC-filtered).

    A Collection is a user-uploaded set of files Agnes has indexed. Returns a
    dict with a ``collections`` list (``id``, ``name``, ``slug``, counts). Use
    ``collection_get`` for the files inside one collection.
    """
    try:
        return api_get_json("/api/collections")
    except V2ClientError as exc:
        raise ValueError(_mcp_error("collections_list", exc)) from exc


@mcp.tool()
def collection_get(collection_id: str) -> dict:
    """Show one Collection's detail plus its files and per-file status.

    Args:
        collection_id: Collection id from ``collections_list`` (``col_...``).
    """
    try:
        return api_get_json(f"/api/collections/{collection_id}")
    except V2ClientError as exc:
        raise ValueError(_mcp_error("collection_get", exc)) from exc


@mcp.tool()
def schema(table_id: str) -> dict:
    """Show column names, types, and SQL dialect hints for a table.

    Args:
        table_id: Table ID from the catalog (e.g. ``crm_accounts``).

    Returns column list with ``name``, ``type``, ``nullable``,
    ``description``.  Also returns ``sql_flavor``, ``partition_by``,
    ``clustered_by``, and ``where_dialect_hints`` where relevant.

    Call this before writing a query — knowing column types avoids
    casting errors and helps pick the right SQL dialect.
    """
    try:
        return api_get_json(f"/api/v2/schema/{table_id}")
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"schema({table_id})", exc)) from exc


@mcp.tool()
def describe(table_id: str, rows: int = 5) -> dict:
    """Show schema plus sample rows for a table.

    Args:
        table_id: Table ID from the catalog.
        rows:     How many sample rows to return (default 5, max 50).

    Returns ``{"schema": {...}, "sample": {"columns": [...], "rows": [...]}}``
    so you can see real values before writing a query.
    """
    rows = min(max(1, rows), 50)
    try:
        sch = api_get_json(f"/api/v2/schema/{table_id}")
        sam = api_get_json(f"/api/v2/sample/{table_id}", n=rows)
        return {"schema": sch, "sample": sam}
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"describe({table_id})", exc)) from exc


@mcp.tool()
def query(sql: str, limit: int = 1000) -> dict:
    """Execute a SQL query against Agnes data.

    For ``query_mode=local`` and ``materialized`` tables the query runs
    against the server-side DuckDB view.  For ``query_mode=remote``
    (BigQuery) it passes through to BigQuery.

    Args:
        sql:   SQL statement to execute.  Use DuckDB dialect for local /
               materialized tables; BigQuery dialect for remote tables
               (check ``sql_flavor`` in the catalog entry).
        limit: Maximum rows to return (default 1000).

    Returns ``{"columns": [...], "rows": [[...], ...], "truncated": bool}``.

    Tips:
    - Always run ``catalog()`` first to know what tables exist.
    - Run ``schema(table_id)`` before writing a query — column names
      and types are essential.
    - Prefer filtered queries over ``SELECT *`` — remote tables can be
      very large.
    """
    try:
        return api_post_json("/api/query", {"sql": sql, "limit": limit})
    except V2ClientError as exc:
        raise ValueError(_mcp_error("query", exc)) from exc


@mcp.tool()
def query_local(sql: str, limit: int = 1000) -> dict:
    """Execute a SQL query directly against the local DuckDB cache.

    Use this for ``query_mode=local`` / ``materialized`` tables after
    ``pull()`` has synced data to disk.  Runs entirely offline — no
    server request is made.

    Args:
        sql:   DuckDB-flavoured SQL.
        limit: Maximum rows to return (default 1000).

    Returns ``{"columns": [...], "rows": [[...], ...]}`` or raises if
    the local DuckDB file does not exist (run ``pull()`` first).
    """

    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()
    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(f"Local DuckDB not found at {db_path}. Run pull() first to sync data.")

    with _open_duckdb(str(db_path), read_only=True) as conn:
        # Apply LIMIT at the DuckDB level to protect against accidental
        # full-table scans on large cached parquets.
        wrapped = f"SELECT * FROM ({sql}) AS _q LIMIT {limit}"
        result = conn.execute(wrapped)
        columns = [d[0] for d in result.description]
        rows = result.fetchall()

    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": len(rows) == limit,
    }


@mcp.tool()
def pull(skip_materialize: bool = False) -> dict:
    """Sync the latest data from the Agnes server to local disk.

    Downloads parquets for all ``local`` and ``materialized`` tables
    visible to your account (RBAC-filtered), then rebuilds the local
    DuckDB view so ``query_local`` picks up the changes.

    Args:
        skip_materialize: Skip large materialized-mode (scheduled BQ
            export) tables — useful for a fast first sync when you only
            need remote-mode access.

    Returns a summary: ``{"tables_updated": N, "parquets_total": N,
    "errors": [...], "duration_s": N}``.

    Run at the start of a session to make sure local data is fresh.
    Equivalent to ``agnes pull`` on the command line.
    """

    server_url = get_server_url()
    token = get_token()
    if not token:
        raise ValueError("No Agnes token configured. Run setup.py from Terminal to authenticate.")

    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()
    result = run_pull(
        server_url,
        token,
        workspace,
        skip_materialize=skip_materialize,
        show_progress=False,
    )
    return {
        "tables_updated": result.tables_updated,
        # Surface prune counts so MCP clients can detect that tables were
        # removed from the workspace (security-relevant — revokes local
        # query access). Was missing in the original #594 (Devin Review).
        "tables_removed": result.tables_removed,
        "parquets_total": result.parquets_total,
        "errors": result.errors,
        # `PullResult.duration_s` is the wall-clock duration of the call.
        # Was historically referenced here as `result.elapsed_s` with a
        # `hasattr` guard that always returned False — every MCP `pull`
        # response returned `"elapsed_s": None` regardless of how long
        # the call took (Devin Review BUG_0001 on #594). Renamed the key
        # to `duration_s` to match `PullResult` + `--json` output.
        "duration_s": round(result.duration_s, 1),
    }


def run() -> None:
    """Entry point — start the MCP server (stdio transport).

    Before binding stdio we ask the configured Agnes server for the set of
    passthrough MCP tools the caller's groups can see, and dynamically
    register one FastMCP tool per entry that forwards through the server's
    ``/api/mcp/passthrough/tools/{tool_id}/call`` endpoint. Best-effort —
    a server outage or pre-Phase-2 image leaves the static tools above
    untouched (the dynamic helper logs to stderr and returns []).
    """
    try:
        # Local import keeps the module's top-level import surface light
        # for callers that only need the static tools (or import this
        # module for testing).
        from cli.mcp._dynamic_passthrough import register_passthrough_tools

        register_passthrough_tools(mcp)
    except Exception as exc:
        # Never let dynamic-registration explode the whole stdio surface.
        import sys as _sys

        print(f"[agnes mcp] dynamic passthrough registration skipped: {exc}", file=_sys.stderr)
    mcp.run()


if __name__ == "__main__":
    run()
