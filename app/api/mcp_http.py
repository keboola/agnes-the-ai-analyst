"""Agnes HTTP MCP server — SSE transport for cowork VM access.

Mounted at /api/mcp in app/main.py. Exposes the same server-side tools as
the stdio MCP server but over HTTP, so Claude Desktop's cowork VM (which
cannot reach localhost) can connect when Agnes is deployed with a public URL.

Authentication: Bearer token in the Authorization header, or ?token= query
param for clients that cannot send headers on SSE GET requests. The token is
validated the same way as every other Agnes API endpoint (JWT + DB PAT check).

Cowork bundle settings.json points to:
    {server_url}/api/mcp/sse

with header  Authorization: Bearer <PAT>  set by Claude Code.

Tools available: server_info, catalog, schema, describe, query, skills.
(query_local and pull require a local analyst filesystem — not available
 in the server context.)
"""
from __future__ import annotations

import contextvars
import logging
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Per-request token — set by _AuthMiddleware, read by tool handlers.
_current_token: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_mcp_token", default=""
)

# Internal base URL for self-calls. Stays on HTTP/localhost since the MCP
# server runs inside the same process/container as Agnes.
_BASE = os.environ.get("AGNES_BASE_URL", "http://localhost:8000").rstrip("/")

mcp = FastMCP(
    "Agnes",
    instructions=(
        "Agnes is an AI Data Analyst platform. "
        "Use `catalog` first to discover available tables, then `schema` to "
        "understand columns, `describe` for sample rows, and `query` to run SQL. "
        "Run `server_info` to check connectivity at the start of a session."
    ),
    # DNS rebinding protection is redundant — _AuthMiddleware validates PAT
    # before any request reaches FastMCP, so the protection is already in place.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _headers() -> dict[str, str]:
    token = _current_token.get()
    if not token:
        raise RuntimeError("No authentication token in current MCP context")
    return {"Authorization": f"Bearer {token}"}


# ── tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def server_info() -> dict:
    """Return Agnes server health and your account email.

    Useful as a quick connectivity check at the start of a session.
    """
    result: dict[str, Any] = {"authenticated": True}
    async with httpx.AsyncClient() as c:
        try:
            r = await c.get(f"{_BASE}/api/health", timeout=5)
            if r.status_code == 200:
                result["health"] = r.json()
        except Exception:
            result["health"] = "unreachable"
        try:
            r = await c.get(f"{_BASE}/api/me", headers=_headers(), timeout=5)
            if r.status_code == 200:
                result["user_email"] = r.json().get("email", "")
        except Exception:
            pass
    return result


@mcp.tool()
async def catalog() -> dict:
    """List all tables available to you (RBAC-filtered).

    Returns a dict with a ``tables`` list.  Each entry has:
    - ``id``         — use this in schema / describe / query calls
    - ``name``       — human-readable label
    - ``query_mode`` — local | remote | materialized
    - ``sql_flavor`` — duckdb or bigquery (affects SQL dialect in query)
    - ``rows``       — approximate row count (may be null)

    Always call this first so you know what data is available.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{_BASE}/api/v2/catalog", headers=_headers(), timeout=30)
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def schema(table_id: str) -> dict:
    """Show column names, types, and SQL dialect hints for a table.

    Args:
        table_id: Table ID from the catalog (e.g. ``crm_accounts``).

    Returns column list with name, type, nullable, description plus
    sql_flavor and where_dialect_hints where relevant.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/v2/schema/{table_id}", headers=_headers(), timeout=30
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def describe(table_id: str, rows: int = 5) -> dict:
    """Show schema plus sample rows for a table.

    Args:
        table_id: Table ID from the catalog.
        rows:     How many sample rows to return (default 5, max 50).

    Returns ``{"schema": {...}, "sample": {"columns": [...], "rows": [...]}}``.
    """
    rows = min(max(1, rows), 50)
    async with httpx.AsyncClient() as c:
        rs = await c.get(
            f"{_BASE}/api/v2/schema/{table_id}", headers=_headers(), timeout=30
        )
        rs.raise_for_status()
        rm = await c.get(
            f"{_BASE}/api/v2/sample/{table_id}",
            headers=_headers(),
            params={"n": rows},
            timeout=30,
        )
        rm.raise_for_status()
    return {"schema": rs.json(), "sample": rm.json()}


@mcp.tool()
async def query(sql: str, limit: int = 1000) -> dict:
    """Execute a SQL query against Agnes data.

    For local and materialized tables the query runs against the server-side
    DuckDB view.  For remote (BigQuery) tables it passes through to BigQuery.

    Args:
        sql:   SQL statement.  Use DuckDB dialect for local/materialized;
               BigQuery dialect for remote tables (check sql_flavor in catalog).
        limit: Maximum rows to return (default 1000).

    Returns ``{"columns": [...], "rows": [[...], ...], "truncated": bool}``.
    """
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{_BASE}/api/query",
            json={"sql": sql, "limit": limit},
            headers=_headers(),
            timeout=60,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def skills() -> dict:
    """List all skills from marketplace plugins you are authorised to access.

    Returns a ``skills`` list.  Each entry has:
    - ``marketplace_id`` — marketplace slug
    - ``plugin_name``    — plugin directory name
    - ``skill_name``     — skill directory name (unique invocation key)
    - ``name``           — human-readable label
    - ``description``    — short description (may be null)
    - ``invocation``     — slash-command or invocation hint (may be null)
    - ``body``           — full SKILL.md text with frontmatter stripped

    Load a ``body`` into your context when you need to follow that skill's
    instructions.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/v2/marketplace/skills",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


# ── auth middleware ─────────────────────────────────────────────────────────────

class _AuthMiddleware:
    """Pure ASGI middleware: validates Bearer token, sets _current_token."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode()

        # Fallback: ?token= query param for clients that can't set headers on SSE GET
        if not auth.lower().startswith("bearer "):
            from urllib.parse import parse_qs
            qs = parse_qs(scope.get("query_string", b"").decode())
            t = qs.get("token", [""])[0]
            if t:
                auth = f"bearer {t}"

        if not auth.lower().startswith("bearer "):
            await _send_401(scope, send)
            return

        raw_token = auth[7:]
        try:
            from app.auth.pat_resolver import resolve_token_to_user
            from src.db import get_system_db
            conn = get_system_db()
            try:
                user, reason = resolve_token_to_user(conn, raw_token)
            finally:
                conn.close()
        except Exception:
            logger.exception("MCP auth error")
            await _send_401(scope, send)
            return

        if user is None:
            await _send_401(scope, send)
            return

        tok = _current_token.set(raw_token)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_token.reset(tok)


async def _send_401(scope: Scope, send: Send) -> None:
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [[b"content-type", b"application/json"]],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"detail":"Not authenticated"}',
        "more_body": False,
    })


# ── dynamic tool registration (Universal MCP — RFC #461 §7) ───────────────────

def _register_dynamic_tools() -> None:
    """Add passthrough tools from ``tool_registry`` to the module-level ``mcp``.

    Called once from ``make_sse_app`` at app startup. Best-effort — if the
    DB is unreachable or the v61 tables are missing, log and skip so the
    cowork MCP server still comes up with the static tools.
    """
    try:
        from app.api.mcp.tools_generator import register_passthrough_tools
        from src.db import get_system_db
    except Exception:  # pragma: no cover - import-time defensive
        logger.exception("Universal MCP imports unavailable; skipping dynamic tool registration")
        return
    try:
        conn = get_system_db()
    except Exception:
        logger.warning("system DB not ready; skipping dynamic passthrough tool registration")
        return
    try:
        names = register_passthrough_tools(mcp, conn)
        if names:
            logger.info("MCP HTTP: registered %d passthrough tools", len(names))
    except Exception:
        logger.exception("Universal MCP passthrough registration failed")
    finally:
        conn.close()


# ── factory ────────────────────────────────────────────────────────────────────

def make_sse_app() -> ASGIApp:
    """Return the Agnes SSE MCP app wrapped with PAT authentication."""
    _register_dynamic_tools()
    return _AuthMiddleware(mcp.sse_app())
