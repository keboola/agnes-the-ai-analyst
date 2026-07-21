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

import httpx
from mcp.server.fastmcp import FastMCP

from cli.client import api_get
from cli.config import get_server_url, get_token
from cli.query_hints import missing_table, remote_table_hint
from cli.v2_client import V2ClientError, api_get_json, api_post_json
from src.duckdb_conn import _open_duckdb

mcp = FastMCP(
    "Agnes",
    instructions=(
        "Agnes is a self-hosted AI harness for the organization's data, skills, and memory. "
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
    dict with an ``items`` list (``id``, ``name``, ``slug``, counts). Use
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
def collections_search(query: str, k: int = 10, collection_id: str = "") -> dict:
    """Hybrid search across your accessible file Collections (RBAC-filtered).

    Returns ranked chunks with citations (``filename``, ``ordinal``, ``text``,
    ``score``). Optionally restrict to one collection via ``collection_id``.

    The response's ``retrieval`` field says how results were ranked:
    ``hybrid`` (lexical + semantic) or ``lexical_only`` — the degraded mode
    when the server has no embedding model installed.
    """
    params: dict = {"q": query, "k": k}
    if collection_id:
        params["corpus_id"] = collection_id
    try:
        return api_get_json("/api/collections/search", **params)
    except V2ClientError as exc:
        raise ValueError(_mcp_error("collections_search", exc)) from exc


@mcp.tool()
def knowledge_search(query: str, k: int = 10) -> dict:
    """One query across documents, the knowledge base, and the data catalog.

    Fans out server-side over Collections chunks (hybrid lexical+vector),
    corporate-memory knowledge items (fulltext), and table catalog cards —
    all RBAC-filtered. Results are typed ``chunk | knowledge | table``;
    a ``table`` hit means structured data: pivot to SQL via the ``query``
    tool with the hit's ``table_id`` instead of reading text chunks.

    The response's ``retrieval`` field labels the chunk engine's mode:
    ``hybrid`` (lexical + semantic) or ``lexical_only`` — the degraded mode
    when no embedding model is installed where the ranking ran.

    Offline fallback (K3, #798): if the server is unreachable (network/VPN
    down), falls back to `agnes pull`-shipped knowledge artifacts under
    `user/knowledge/` and runs the same hybrid ranking locally — documents
    (``chunk``) only, no ``knowledge``/``table`` hits. The response then
    carries ``source: "local"`` and a ``note`` explaining the degradation.
    An HTTP error from a reachable server (``V2ClientError``) is NOT a
    fallback trigger — the server answered, its error is the truth.
    """
    try:
        return api_get_json("/api/knowledge/search", q=query, k=k)
    except V2ClientError as exc:
        raise ValueError(_mcp_error("knowledge_search", exc)) from exc
    except httpx.TransportError as exc:
        # Server unreachable (offline laptop, VPN down) — fall back to the
        # artifacts `agnes pull` shipped. Same hybrid scoring, chunk source
        # only; HTTP errors above do NOT fall back (the server answered).
        from cli.config import get_workspace_root
        from src.search.local import local_search

        ws = get_workspace_root()
        if not ws:
            raise ValueError(
                f"knowledge_search failed: server unreachable ({exc}) and no "
                "local workspace configured — run `agnes init` + `agnes pull`."
            ) from exc
        from src.ingest.retrieval import retrieval_mode

        results = local_search(query, workspace=Path(ws), k=k)
        return {
            "query": query,
            "results": results,
            # Mode of the LOCAL ranking that just ran — the laptop may lack
            # the embeddings extra even when the server has it.
            "retrieval": retrieval_mode(),
            "source": "local",
            "note": "server unreachable — searched local knowledge artifacts (documents only)",
        }


@mcp.tool()
def collections_reingest(collection_id: str, file_id: str) -> dict:
    """Re-run ingestion for one file in a Collection (requires access to the collection).

    Use after the file or extraction config was fixed — e.g. a file stuck
    in ``needs_review`` (empty extraction) or ``rejected``. Returns the file
    row reset to ``pending``; ingestion runs server-side in the background.

    Args:
        collection_id: Collection id from ``collections_list`` (``col_...``).
        file_id: File id from ``collection_get`` (``cf_...``).
    """
    try:
        return api_post_json(f"/api/collections/{collection_id}/files/{file_id}/reingest", {})
    except V2ClientError as exc:
        raise ValueError(_mcp_error("collections_reingest", exc)) from exc


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

    Returns ``{"schema": {...}, "sample": {"table_id": ..., "rows": [...],
    "source": ...}}`` where ``sample.rows`` is a list of ``{column: value}``
    objects (empty when the table has no rows — there is no ``columns`` key;
    column names come from ``schema.columns``), so you can see real values
    before writing a query.
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

    Routes local/materialized vs remote tables automatically server-side;
    prefer this tool unless you specifically need offline access.

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

    If a table is missing here it may be a ``query_mode='remote'`` or
    ``server_only`` table — use the ``query`` tool instead.

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
        try:
            result = conn.execute(wrapped)
            columns = [d[0] for d in result.description]
            rows = result.fetchall()
        except Exception as exc:
            table = missing_table(str(exc))
            if table:
                raise ValueError(f"query_local failed: {exc}\n{remote_table_hint(table, surface='mcp')}") from exc
            raise

    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": len(rows) == limit,
    }


@mcp.tool()
def chat_upload_file(
    file_path: str,
    kind: str = "data",
    register_as_table: bool = False,
    table_name: str = "",
) -> dict:
    """Upload a local file into your chat workspace (POST /api/chat/uploads).

    The file at ``file_path`` (local filesystem path) is read and posted to
    the Agnes server, landing in your per-user workspace ``uploads/`` folder
    so Claude can access it in the next chat sandbox session.

    For data files (CSV, parquet, XLSX) set ``register_as_table=True`` to
    register the file as a workspace-local queryable table so
    ``agnes query`` can reach it without an admin table-registry entry.

    Args:
        file_path: Local path to the file to upload.
        kind: One of ``data``, ``image``, ``document``. Default ``data``.
        register_as_table: When True (data files only), register the uploaded
            file as a workspace-local queryable table.
        table_name: Optional table name for registration. Derived from the
            filename stem when omitted.

    Returns the upload response with ``workspace_path``, ``filename``,
    ``size_bytes``, ``kind``, ``table_name`` (if registered), and ``hint``.

    Mirrors ``POST /api/chat/uploads`` and ``agnes chat upload``.
    """
    import mimetypes

    path = Path(file_path)
    if not path.exists():
        raise ValueError(f"File not found: '{file_path}'. Check the path and try again.")

    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    data: dict[str, str] = {"kind": kind}
    if register_as_table:
        data["register_as_table"] = "true"
    if table_name:
        data["table_name"] = table_name

    server_url = get_server_url()
    token = get_token()
    if not token:
        raise ValueError("No Agnes token configured. Run setup.py from Terminal to authenticate.")

    import httpx

    with path.open("rb") as fh, httpx.Client() as c:
        r = c.post(
            f"{server_url}/api/chat/uploads",
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            files={"file": (path.name, fh, content_type)},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()


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
    # Imported inside the function (not at module scope) so tests can patch
    # ``cli.lib.pull.run_pull`` and have the patch take effect at call time.
    from cli.lib.pull import run_pull

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
