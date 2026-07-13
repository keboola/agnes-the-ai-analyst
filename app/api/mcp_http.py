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

Tools available: the 24 foundation tools registered by
``app/api/mcp/foundation_tools.py`` — server_info, catalog, collections_list,
collection_get, collections_search, knowledge_search, collections_reingest,
schema, describe, query, skills, chat_skills, stack_browse, stack_subscribe,
stack_unsubscribe, store_rate, store_status, store_publish_markdown,
documentation_api, list_contributed_skills, contribute_skill,
delete_contributed_skill, admin_config_surface, admin_source_connections_list.
(query_local and pull require a local analyst filesystem — not available
 in the server context.)
"""

from __future__ import annotations

import contextvars
import logging
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.mcp.foundation_tools import register_foundation_tools

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
        "Agnes is a self-hosted AI harness for the organization's data, skills, and memory. "
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


_FOUNDATION_TOOL_NAMES = register_foundation_tools(mcp, base_url=_BASE, headers_fn=_headers)

# Back-compat: bind each registered tool function onto this module's globals.
# Existing unit tests call e.g. ``mcp_http.catalog(...)`` directly to exercise
# tool logic without going through the MCP protocol layer; FastMCP's
# @mcp.tool() decorator returns the original function unchanged, so the
# implementation lives in foundation_tools.py but stays reachable here.
for _name in _FOUNDATION_TOOL_NAMES:
    globals()[_name] = mcp._tool_manager.get_tool(_name).fn
del _name


@mcp.tool()
async def admin_knowledge_digests_list() -> dict:
    """List all maintained digests (admin only).

    A maintained digest is an admin-defined markdown document — title +
    standing instructions + a set of source Collections — that the scheduler
    regenerates with an LLM only when its sources' content changes. Access to
    a digest's content is controlled by ``resource_grants`` on the
    ``knowledge_digest`` resource type: a grant is what makes ``agnes pull``
    deliver the digest to a group's members as ``.claude/rules/ka_<slug>.md``.

    Returns ``{"items": [{"id", "slug", "title", "status",
    "status_reason", "generated_at", "output_md" (280-char preview),
    "output_chars"}, ...]}``. Mirrors ``GET /api/admin/knowledge-digests``
    and ``agnes admin digest list``.

    Requires an admin PAT.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/admin/knowledge-digests",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def admin_knowledge_digest_get(digest_id: str) -> dict:
    """Show one maintained digest's full detail (admin only).

    Includes the full ``output_md`` (the list tool only ships a preview) and
    the staleness fields: a digest whose sources changed but whose last
    regeneration failed is ``status: "stale"`` with a ``status_reason`` —
    the previous markdown is kept and still distributed, never silently.

    Args:
        digest_id: The digest id (from ``admin_knowledge_digests_list``).

    Mirrors ``GET /api/admin/knowledge-digests/{id}`` and
    ``agnes admin digest show``. Requires an admin PAT.
    """
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{_BASE}/api/admin/knowledge-digests/{digest_id}",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def admin_knowledge_digest_create(
    slug: str,
    title: str,
    instructions: str,
    source_corpus_ids: list[str] | None = None,
) -> dict:
    """Create a new maintained digest (admin only).

    The digest starts ``status: "pending"`` — no markdown is generated until
    the next scheduler pass fingerprints the source Collections and runs the
    LLM regeneration. Granting a group access (``agnes admin grant create
    <group> knowledge_digest <digest_id>``) is what makes ``agnes pull``
    deliver it to that group's members as ``.claude/rules/ka_<slug>.md``.

    Args:
        slug:              URL-safe stable id — becomes the filename
                            ``ka_<slug>.md`` on every analyst laptop.
                            Immutable after create.
        title:              Display title.
        instructions:       Standing instructions for the LLM regeneration
                             pass (what the digest should cover / how).
        source_corpus_ids:  Ids of the source Collections to fingerprint and
                             summarize. Defaults to none.

    Mirrors ``POST /api/admin/knowledge-digests`` and
    ``agnes admin digest create``. Requires an admin PAT.
    """
    payload = {
        "slug": slug,
        "title": title,
        "instructions": instructions,
        "source_corpus_ids": source_corpus_ids or [],
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{_BASE}/api/admin/knowledge-digests",
            json=payload,
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def admin_knowledge_digest_update(
    digest_id: str,
    title: str | None = None,
    instructions: str | None = None,
    source_corpus_ids: list[str] | None = None,
) -> dict:
    """Update a maintained digest's metadata (admin only).

    Only the supplied fields change; the slug is immutable (it's already a
    filename on analyst laptops). Editing ``instructions`` or
    ``source_corpus_ids`` flips the digest's content fingerprint, so the next
    scheduler pass regenerates it even if the source Collections themselves
    haven't changed.

    Args:
        digest_id:          The digest id to update.
        title:              New display title, if changing.
        instructions:       New standing instructions, if changing.
        source_corpus_ids:  New full list of source Collection ids, if
                            changing (replaces the previous list).

    Mirrors ``PUT /api/admin/knowledge-digests/{id}`` and
    ``agnes admin digest edit``. Requires an admin PAT.
    """
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if instructions is not None:
        payload["instructions"] = instructions
    if source_corpus_ids is not None:
        payload["source_corpus_ids"] = source_corpus_ids
    async with httpx.AsyncClient() as c:
        r = await c.put(
            f"{_BASE}/api/admin/knowledge-digests/{digest_id}",
            json=payload,
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def admin_knowledge_digest_delete(digest_id: str) -> dict:
    """Delete a maintained digest (admin only).

    Also removes any dangling ``resource_grants`` rows for the digest, so no
    group retains a grant pointing at a now-nonexistent resource. Analyst
    laptops prune the corresponding ``ka_<slug>.md`` on their next
    ``agnes pull``.

    Args:
        digest_id: The digest id to delete.

    Mirrors ``DELETE /api/admin/knowledge-digests/{id}`` and
    ``agnes admin digest delete``. Requires an admin PAT.
    """
    async with httpx.AsyncClient() as c:
        r = await c.delete(
            f"{_BASE}/api/admin/knowledge-digests/{digest_id}",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
    return {"deleted": digest_id}


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
    except Exception:  # pragma: no cover - import-time defensive
        logger.exception("Universal MCP imports unavailable; skipping dynamic tool registration")
        return
    try:
        names = register_passthrough_tools(mcp)
        if names:
            logger.info("MCP HTTP: registered %d passthrough tools", len(names))
    except Exception:
        logger.exception("Universal MCP passthrough registration failed")


# ── factory ────────────────────────────────────────────────────────────────────


def make_sse_app() -> ASGIApp:
    """Return the Agnes SSE MCP app wrapped with PAT authentication."""
    _register_dynamic_tools()
    return _AuthMiddleware(mcp.sse_app())
