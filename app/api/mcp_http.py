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

Tools available: server_info, catalog, schema, describe, query, skills,
stack_browse, stack_subscribe, stack_unsubscribe, store_rate, store_status,
documentation_api.
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
_current_token: contextvars.ContextVar[str] = contextvars.ContextVar("_mcp_token", default="")

# Internal base URL for self-calls. Stays on HTTP/localhost since the MCP
# server runs inside the same process/container as Agnes.
#
# Devin Review on #474 flagged that reusing ``AGNES_BASE_URL`` (the
# public-facing hostname operators set so Cowork VMs can reach Agnes)
# made every self-call here round-trip through the public proxy
# (TLS + reverse-proxy + DNS), adding latency and breaking when the
# external URL isn't resolvable from inside the container (e.g. when
# the reverse proxy is air-gapped from internal traffic). Use a
# dedicated ``AGNES_MCP_INTERNAL_URL`` instead, defaulting to
# ``http://localhost:8000`` — the right shape for self-calls in the
# single-container deploy. Operators running Agnes split across
# multiple pods can point this at the in-cluster service URL.
_BASE = os.environ.get("AGNES_MCP_INTERNAL_URL", "http://localhost:8000").rstrip("/")

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
async def collections_list() -> dict:
    """List the file Collections you can access (RBAC-filtered).

    A Collection is a user-uploaded set of files Agnes has indexed. Returns a
    dict with an ``items`` list; each entry has ``id``, ``name``,
    ``slug``, and file/table counts. Use ``collection_get`` for the files in
    one collection.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{_BASE}/api/collections", headers=_headers(), timeout=30)
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def collection_get(collection_id: str) -> dict:
    """Show one Collection's detail plus its files and per-file status.

    Args:
        collection_id: Collection id from ``collections_list`` (``col_...``).
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/collections/{collection_id}",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def collections_search(query: str, k: int = 10, collection_id: str = "") -> dict:
    """Hybrid search across your accessible file Collections (RBAC-filtered).

    Returns ranked chunks with citations (``filename``, ``ordinal``, ``text``,
    ``score``). Optionally restrict to one collection via ``collection_id``.

    Args:
        query: Natural-language or keyword query.
        k: Max results (default 10).
        collection_id: Optional ``col_...`` id to restrict the search.
    """
    params: dict = {"q": query, "k": k}
    if collection_id:
        params["corpus_id"] = collection_id
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/collections/search",
            headers=_headers(),
            params=params,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def collections_reingest(collection_id: str, file_id: str) -> dict:
    """Re-run ingestion for one file in a Collection (requires access to the collection).

    Use after the file or extraction config was fixed — e.g. a file stuck
    in ``needs_review`` (empty extraction) or ``rejected``. Returns the file
    row reset to ``pending``; ingestion runs server-side in the background.

    Args:
        collection_id: Collection id from ``collections_list`` (``col_...``).
        file_id: File id from ``collection_get`` (``cf_...``).
    """
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{_BASE}/api/collections/{collection_id}/files/{file_id}/reingest",
            json={},
            headers=_headers(),
            timeout=30,
        )
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
        r = await c.get(f"{_BASE}/api/v2/schema/{table_id}", headers=_headers(), timeout=30)
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
        rs = await c.get(f"{_BASE}/api/v2/schema/{table_id}", headers=_headers(), timeout=30)
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


@mcp.tool()
async def chat_skills() -> dict:
    """List skills + slash commands invokable in your web chat sandbox.

    Unlike ``skills`` (every marketplace skill you're RBAC-granted, with full
    bodies), this mirrors what the web chat composer's slash menu shows:
    skills bundled into the chat sandbox's workspace template merged with
    your RBAC-filtered marketplace/store plugin skills (marketplace wins name
    clashes) — the same set ``app/chat/runner.py`` installs into a live
    session. Requires cloud chat to be enabled and granted to you.

    Returns ``{"skills": [{"name", "description", "source"}],
    "commands": [{"name", "description"}]}``. ``commands`` is currently
    always empty — no slash command is backend-recognized yet.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/chat/skills",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def stack_browse(resource_type: str) -> dict:
    """List resources you could add to your stack (RBAC-granted candidates).

    Unlike ``catalog`` (which lists tables already in your stack), this is the
    discovery surface: every data package or memory domain your groups are
    granted, each annotated with an ``in_stack`` flag so you can tell what is
    already subscribed and what is still available to add.

    Args:
        resource_type: ``data_package`` or ``memory_domain``.

    Returns ``{"items": [{"id", "name", "description", "requirement",
    "in_stack", ...}]}``. Subscribe to an available item with
    ``stack_subscribe``.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/stack/browse",
            headers=_headers(),
            params={"type": resource_type},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def stack_subscribe(resource_type: str, resource_id: str) -> dict:
    """Subscribe to an available data package or memory domain.

    Adds the resource to your persistent stack — the same effect as clicking
    "Add to stack" in the web UI; it applies to all future sessions. Use
    ``stack_browse`` first to find the ``resource_id`` of an available
    (``in_stack: false``) item.

    Args:
        resource_type: ``data_package`` or ``memory_domain``.
        resource_id:   The resource id from ``stack_browse``.

    Returns ``{"subscribed": true, "next_step": "..."}`` — ``next_step`` tells
    you what to run so the new resource becomes usable in this conversation.
    """
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{_BASE}/api/stack/subscribe",
            json={"resource_type": resource_type, "resource_id": resource_id},
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
    # Post-subscribe hint — both supported types land as local tables pulled
    # by ``agnes pull`` (data packages → parquet, memory domains → synced
    # knowledge). Tell the model what to run so the resource is usable now.
    if isinstance(body, dict):
        body["next_step"] = "Run `agnes pull` to download the new tables."
    return body


@mcp.tool()
async def stack_unsubscribe(resource_type: str, resource_id: str) -> dict:
    """Unsubscribe from a data package or memory domain in your stack.

    Removes a previously-subscribed resource. Required resources cannot be
    removed (the server returns an error) — only ``available`` ones you opted
    into. The local copy persists until the next ``agnes pull`` prunes it.

    Args:
        resource_type: ``data_package`` or ``memory_domain``.
        resource_id:   The resource id to unsubscribe from.

    Returns ``{"unsubscribed": true}`` on success.
    """
    async with httpx.AsyncClient() as c:
        r = await c.delete(
            f"{_BASE}/api/stack/subscription/{resource_type}/{resource_id}",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
    return {"unsubscribed": True}


@mcp.tool()
async def store_rate(entity_id: str, vote: int) -> dict:
    """Rate a store / marketplace entity thumbs up/down (#398).

    Casts, changes, or clears your single vote on an entity — the same effect
    as the thumbs buttons in the marketplace detail view; one vote per entity
    per user, re-voting replaces the prior value.

    Args:
        entity_id: The store entity id (from ``catalog`` / marketplace browse).
        vote:      ``1`` = thumbs up, ``-1`` = thumbs down, ``0`` = clear your vote.

    Returns ``{"up", "down", "my_vote"}`` — the updated tally for the entity.
    """
    if vote not in (1, -1, 0):
        raise ValueError("vote must be 1, -1, or 0")
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{_BASE}/api/store/entities/{entity_id}/rate",
            json={"vote": vote},
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def store_status(entity_id: str) -> dict:
    """Check the review-pipeline status of a flea-market entity you own.

    After ``store upload`` the guardrail review runs asynchronously; the
    entity stays hidden until it passes. This returns the latest submission's
    status (``pending_llm`` / ``approved`` / ``blocked_llm`` /
    ``review_error`` / ``overridden``) plus an actionable hint. Owner or
    admin only. Mirrors ``agnes store status <id>``.

    Args:
        entity_id: The store entity id (from the upload response).

    Returns the ``GET /api/store/entities/{id}/status`` payload:
    ``{entity_id, name, type, visibility_status, version_no, submission,
    hint}``.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/store/entities/{entity_id}/status",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def documentation_api() -> str:
    """Return the curated Agnes REST API reference as Markdown.

    Mirrors the in-app ``/documentation/api`` page and the ``agnes docs api``
    CLI command — three surfaces in lockstep so a public endpoint is reachable
    everywhere it can be looked up. Useful when an agent is composing a
    request against ``/api/*`` and needs to know payload shapes, auth
    requirements, or the inventory of available endpoints without leaving the
    chat.
    """
    from pathlib import Path

    md_path = Path(__file__).resolve().parent.parent.parent / "docs" / "api-reference.md"
    try:
        return md_path.read_text(encoding="utf-8")
    except OSError:
        return "# API reference unavailable\n\nThe source markdown file is missing from this deployment."


@mcp.tool()
async def list_contributed_skills() -> dict:
    """List all plugins in the Agnes Contributed marketplace (admin only).

    Returns name, version, description, and granted group for each plugin
    contributed via the web form, CLI, or ``contribute_skill`` MCP tool.
    Mirrors ``GET /api/admin/contributed-skills`` and ``agnes admin skill list``.

    Requires an admin PAT.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/admin/contributed-skills",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def contribute_skill(skill_md: str, grant_group: str = "Admin") -> dict:
    """Publish a SKILL.md into the Agnes Contributed marketplace (admin only).

    Parses the SKILL.md frontmatter, wraps the skill in a one-skill plugin,
    and grants it to ``grant_group``. Mirrors ``POST /api/admin/contributed-skills``
    and ``agnes admin skill contribute``.
    """
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{_BASE}/api/admin/contributed-skills",
            json={"skill_md": skill_md, "grant_group": grant_group},
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def delete_contributed_skill(name: str) -> dict:
    """Remove a contributed skill by plugin name (admin only).

    Mirrors ``DELETE /api/admin/contributed-skills/{name}`` and
    ``agnes admin skill delete``.
    """
    async with httpx.AsyncClient() as c:
        r = await c.delete(
            f"{_BASE}/api/admin/contributed-skills/{name}",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return {"deleted": name, "status": r.status_code}


@mcp.tool()
async def admin_config_surface() -> dict:
    """Return the complete per-instance configuration surface (admin only).

    Reads every ``get_*`` resolver in ``app/instance_config.py`` and returns
    their current values alongside which tier supplied each one (env/yaml/default),
    the registered Initial Workspace Template (if any), every registered
    marketplace, and the ``infra_repo_url`` knob.

    Useful for an operator's Claude that needs instance-accurate pointers
    (IWT URL, marketplace URLs, knob values, infra repo) without hardcoding
    anything. Mirrors ``GET /api/admin/config-surface`` and
    ``agnes admin config-surface``.

    Requires an admin PAT.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/admin/config-surface",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def admin_source_connections_list(source_type: str = "") -> dict:
    """List named source connections (multi-project Keboola support).

    Returns all registered source connections. Pass ``source_type="keboola"``
    to filter to Keboola connections only.

    Mirrors ``GET /api/admin/source-connections`` and
    ``agnes admin connection list``.

    Requires an admin PAT.
    """
    async with httpx.AsyncClient() as c:
        params = {"source_type": source_type} if source_type else {}
        r = await c.get(
            f"{_BASE}/api/admin/source-connections",
            headers=_headers(),
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return {"connections": r.json()}


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
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"www-authenticate", b'Bearer realm="Agnes MCP"'],
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b'{"detail":"Not authenticated"}',
            "more_body": False,
        }
    )


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
