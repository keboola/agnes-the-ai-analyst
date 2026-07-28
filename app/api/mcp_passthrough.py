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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.mcp_policy import (
    GrantDenied,
    MutatingNotAllowed,
    PerUserCredentialMissing,
    RateLimited,
    caller_authority,
    connection_scope_ids,
    enforce_passthrough_access,
    enforce_per_user_credential,
    redact_response,
)
from app.auth.access import _user_group_ids
from app.auth.dependencies import get_current_user
from connectors.mcp.client import call_tool_async
from src.repositories import mcp_sources_repo, tool_registry_repo
from src.repositories.tool_registry import PASSTHROUGH

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


def _visible_passthrough_tools(user: Any) -> List[Dict[str, Any]]:
    """List of passthrough tool rows the caller is allowed to see.

    Admin sees every enabled passthrough tool. Non-admin sees the
    intersection of ``tool_grants`` with their ``user_group_members``.

    An ``AgentPrincipal`` (V1d) sees its OWNER's set — with the admin
    short-circuit forced off, so an admin-owned agent never inherits the full
    surface — further narrowed to the MCP sources in its ``connection`` scope
    when its ``connections_mode`` is ``'selected'``. A co-session principal
    has no single owner to resolve credentials or grants from and sees
    nothing. The same ``caller_authority`` / ``connection_scope_ids`` pair
    drives ``enforce_passthrough_access``, so this listing can never advertise
    a tool the call seam would refuse (or hide one it would allow).

    Backend-aware: reads tool_registry through the factory and resolves RBAC
    via ``is_user_admin`` / ``_user_group_ids`` without a connection, so it hits
    the active backend (was a raw DuckDB-conn read that returned nothing on a
    Postgres instance — empty tool list / failed passthrough calls).
    """
    authority = caller_authority(user)
    if not authority.user_id:
        return []
    tools_repo = tool_registry_repo()
    if authority.is_admin:
        rows = tools_repo.list_by_mode(PASSTHROUGH, enabled_only=True)
    else:
        rows = tools_repo.list_passthrough_for_groups(list(_user_group_ids(authority.user_id)))
    allowed_sources = connection_scope_ids(authority)
    if allowed_sources is None:
        return rows
    return [t for t in rows if t.get("source_id") in allowed_sources]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tools", response_model=List[PassthroughToolDTO])
async def list_passthrough_tools(
    user: dict = Depends(get_current_user),
) -> List[PassthroughToolDTO]:
    """List passthrough MCP tools visible to the caller.

    Used by the stdio MCP server (``agnes mcp``) at startup so analyst
    workspaces dynamically gain access to upstream MCP tools their admin
    has curated and granted to their groups.
    """
    # Index sources once so each tool can resolve its source name cheaply.
    source_names: Dict[str, str] = {s["id"]: s["name"] for s in mcp_sources_repo().list_all(enabled_only=True)}
    out: List[PassthroughToolDTO] = []
    for tool in _visible_passthrough_tools(user):
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
) -> InvokeResponse:
    """Forward a tool call to the upstream MCP source and return its content.

    RBAC: admin short-circuit; otherwise the caller must be in a group
    listed in ``tool_grants`` for this tool. An agent-session caller (V1d)
    runs on its owner's grants minus the admin short-circuit, and only on the
    MCP sources in its ``connection`` scope — enforced HERE, at the call seam,
    not merely by omission from ``/tools``.
    """
    authority = caller_authority(user)
    if not authority.user_id:
        # A restricted principal with no single owner (co-session) — nothing
        # to resolve grants or per-user credentials from. Fail closed.
        raise HTTPException(status_code=403, detail="not available to this token")
    tools_repo = tool_registry_repo()
    tool = tools_repo.get(tool_id)
    if tool is None or tool.get("mode") != PASSTHROUGH or not tool.get("enabled", True):
        raise HTTPException(status_code=404, detail="passthrough tool not found")

    # Authorization + policy gates (RFC #461 §3), via the one gate stack shared
    # with the SSE / Streamable-HTTP transport closures (app/api/mcp/
    # tools_generator) so the interactive-forward paths can't drift: grant →
    # mutating → connection scope → rate-limit. PII redaction is applied
    # *after* a successful forward, below.
    try:
        enforce_passthrough_access(tool, user)
    except (GrantDenied, MutatingNotAllowed) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(int(exc.retry_after_seconds) + 1)},
        ) from exc

    sources_repo = mcp_sources_repo()
    source = sources_repo.get(tool["source_id"])
    if source is None or not source.get("enabled", True):
        raise HTTPException(status_code=409, detail="upstream MCP source missing or disabled")

    # Fail-closed guard for per-user sources — shared with the SSE / Streamable
    # transport closures (app/api/mcp/tools_generator) so the pre-forward guard
    # can't drift: an identified caller on a per_user source must have their own
    # stored credential, else the forward would connect anonymously and degrade
    # to an opaque upstream auth error.
    try:
        enforce_per_user_credential(source, authority.user_id)
    except PerUserCredentialMissing as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    try:
        # Thread the caller's user_id through so sources with
        # ``scope='per_user'`` resolve the analyst's own credential. An agent
        # forwards under its OWNER's credential — it holds none of its own,
        # and the connection it is using is one the owner authorized above.
        result = await call_tool_async(
            source,
            tool["original_name"],
            arguments=body.arguments,
            caller_user_id=authority.user_id,
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
