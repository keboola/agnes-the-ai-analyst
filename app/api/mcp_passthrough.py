"""User-facing REST surface for the inbound MCP passthrough tools.

Two endpoints, both gated by ``get_current_user`` (any authenticated user),
with per-tool RBAC enforced via ``tool_grants`` + ``user_group_members``
(admin short-circuits). They power three callers:

* ``cli/mcp/server.py`` — the stdio MCP server on an analyst laptop.
  At startup it ``GET``s ``/api/mcp/passthrough/tools``, dynamically
  registers a FastMCP tool per entry, and routes calls to
  ``POST /api/mcp/passthrough/tools/{tool_id}/call``.
* ``app/api/mcp_http.py`` — the SSE-mounted FastMCP server already
  registers passthrough tools statically at app startup, but the same
  REST surface here lets non-MCP clients (web UI, scripts) trigger a
  forward without going through SSE.
* External AI assistants connected to Agnes via a PAT can call
  ``/call`` directly to forward a single invocation.

The ``/call`` endpoint forwards to the upstream MCP source via
``connectors/mcp/client.call_tool_async``; auth_method + auth_secret_env
on ``mcp_sources`` decide what the upstream sees. RFC #461 §4 vault +
per-user credential passthrough is the next step — see
``dev_docs/POC-mcp-universal.md`` "Known limitations".
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.mcp_policy import (
    MutatingNotAllowed,
    RateLimited,
    check_mutating,
    check_rate_limit,
    redact_response,
)
from app.auth.access import _user_group_ids, is_user_admin
from app.auth.dependencies import _get_db, get_current_user
from connectors.mcp.client import call_tool_async
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp/passthrough", tags=["mcp-passthrough"])


# ---------------------------------------------------------------------------
# Response/request models
# ---------------------------------------------------------------------------


class PassthroughToolDTO(BaseModel):
    """Slimmed-down tool_registry row for the stdio client's tool list."""
    tool_id: str
    source_id: str
    source_name: str
    exposed_name: str
    description: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None


class InvokeRequest(BaseModel):
    arguments: Dict[str, Any] = {}


class InvokeResponse(BaseModel):
    is_error: bool
    text: str
    data: Optional[Any] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_dto(tool: Dict[str, Any], source_name: str) -> PassthroughToolDTO:
    input_schema = tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else None
    return PassthroughToolDTO(
        tool_id=tool["tool_id"],
        source_id=tool["source_id"],
        source_name=source_name,
        exposed_name=tool["exposed_name"],
        description=tool.get("description"),
        input_schema=input_schema,
    )


def _visible_passthrough_tools(
    user: Dict[str, Any],
    conn: duckdb.DuckDBPyConnection,
) -> List[Dict[str, Any]]:
    """List of passthrough tool rows the caller is allowed to see.

    Admin sees every enabled passthrough tool. Non-admin sees the
    intersection of ``tool_grants`` with their ``user_group_members``.
    """
    tools_repo = ToolRegistryRepository(conn)
    if is_user_admin(user["id"], conn):
        return tools_repo.list_by_mode(PASSTHROUGH, enabled_only=True)
    group_ids = list(_user_group_ids(user["id"], conn))
    return tools_repo.list_passthrough_for_groups(group_ids)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tools", response_model=List[PassthroughToolDTO])
async def list_passthrough_tools(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> List[PassthroughToolDTO]:
    """List passthrough MCP tools visible to the caller.

    Used by the stdio MCP server (``agnes mcp``) at startup so analyst
    workspaces dynamically gain access to upstream MCP tools their admin
    has curated and granted to their groups.
    """
    sources_repo = MCPSourceRepository(conn)
    # Index sources once so each tool can resolve its source name cheaply.
    source_names: Dict[str, str] = {
        s["id"]: s["name"] for s in sources_repo.list_all(enabled_only=True)
    }
    out: List[PassthroughToolDTO] = []
    for tool in _visible_passthrough_tools(user, conn):
        source_name = source_names.get(tool["source_id"])
        if source_name is None:
            # Source disabled or absent — skip silently, matches the
            # behavior of ``tools_generator.register_passthrough_tools``.
            continue
        out.append(_to_dto(tool, source_name))
    return out


@router.post("/tools/{tool_id}/call", response_model=InvokeResponse)
async def invoke_passthrough_tool(
    tool_id: str,
    body: InvokeRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> InvokeResponse:
    """Forward a tool call to the upstream MCP source and return its content.

    RBAC: admin short-circuit; otherwise the caller must be in a group
    listed in ``tool_grants`` for this tool.
    """
    tools_repo = ToolRegistryRepository(conn)
    tool = tools_repo.get(tool_id)
    if tool is None or tool.get("mode") != PASSTHROUGH or not tool.get("enabled", True):
        raise HTTPException(status_code=404, detail="passthrough tool not found")

    caller_is_admin = is_user_admin(user["id"], conn)
    if not caller_is_admin:
        group_ids = list(_user_group_ids(user["id"], conn))
        if not tools_repo.is_granted_to_groups(tool_id, group_ids):
            raise HTTPException(
                status_code=403,
                detail=f"no grant on tool {tool_id!r} for your groups",
            )

    # Policy gates (RFC #461 §3). Order matters: cheap-and-decisive
    # mutating gate first, then rate-limit (also cheap), then forward.
    # PII redaction is applied *after* a successful forward.
    try:
        check_mutating(tool, is_admin=caller_is_admin)
    except MutatingNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        check_rate_limit(tool_id, user["id"], tool.get("rate_limit_pm"))
    except RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(int(exc.retry_after_seconds) + 1)},
        ) from exc

    sources_repo = MCPSourceRepository(conn)
    source = sources_repo.get(tool["source_id"])
    if source is None or not source.get("enabled", True):
        raise HTTPException(status_code=409, detail="upstream MCP source missing or disabled")

    try:
        # Thread the caller's user_id through so sources with
        # ``scope='per_user'`` resolve the analyst's own credential.
        result = await call_tool_async(
            source,
            tool["original_name"],
            arguments=body.arguments,
            caller_user_id=user["id"],
        )
    except Exception as exc:
        logger.exception("passthrough call to %s failed", tool_id)
        # 502 — Agnes IS reachable, but the upstream MCP we're proxying isn't.
        raise HTTPException(status_code=502, detail=f"upstream call failed: {exc}") from exc

    redacted_text, redacted_data = redact_response(
        text=result.text,
        data=result.data,
        pii_fields=tool.get("pii_fields") if isinstance(tool.get("pii_fields"), list) else None,
    )
    return InvokeResponse(is_error=result.is_error, text=redacted_text, data=redacted_data)
