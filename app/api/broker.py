"""Chat sandbox secret broker routes (2026-07-14 incident hardening).

The in-sandbox loopback relay (``app/chat/relay.py``) never holds a real
credential — it forwards CLI/MCP traffic to these routes carrying only an
opaque, short-lived ticket (``src/repositories/ticket.py``). These routes:

- ``POST /api/broker/anthropic`` — inject the real ``ANTHROPIC_API_KEY``
  server-side and forward to the pinned Anthropic API. The agent-supplied
  request never carries (or can redirect) the real key or host.
- ``POST /api/broker/agnes-api`` / ``POST /api/broker/agnes-mcp`` — resolve
  the ticket to the caller's real Agnes identity, mint an ordinary session
  JWT for that identity, and replay the described ``{method, path, body}``
  request in-process through the *same* FastAPI app instance that received
  the broker call (``request.app``) via ``httpx.ASGITransport``. This keeps
  every access-control check (RBAC, admin gates, resource grants) exactly as
  live as a direct call — the broker adds no privilege of its own.

Ticket scope ("main" vs "mcp") must match the route: a ticket minted for one
CLI cannot be replayed against the other's route. Admin-mutation paths
(``/api/admin/*``) are hard-rejected — the broker only ever re-authenticates
the interactive-parity flows (catalog reads, queries, MCP tool calls), never
privileged admin writes, regardless of the resolved identity's own grants.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth.jwt import create_access_token
from src.repositories import audit_repo, chat_session_repo, ticket_repo, users_repo

router = APIRouter(prefix="/api/broker", tags=["broker"])

# Admin mutations are never brokered — the broker replays only the
# interactive-parity surface (catalog/query/MCP), never admin writes,
# regardless of the resolved identity's own grants.
_ADMIN_PATH_PREFIX = "/api/admin/"

# Anthropic traffic is always forwarded to this pinned host — the sandbox's
# request never gets to choose where its "anthropic" call actually goes.
_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


async def require_broker_ticket(request: Request) -> Dict[str, Any]:
    """Resolve the bearer ticket on the request. 401s if missing/unknown/expired."""
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="missing_broker_ticket")
    row = ticket_repo().resolve(token)
    if row is None:
        raise HTTPException(status_code=401, detail="invalid_or_expired_ticket")
    return row


def _require_scope(row: Dict[str, Any], scope: str) -> None:
    """Hard-deny (401) + audit a ticket presented against the wrong-scope route.

    A ticket minted for the MCP loopback must never authenticate the main
    CLI's broker route and vice versa — the spawn-time scope is the
    contract, not the identity behind it.
    """
    if row.get("scope") != scope:
        try:
            audit_repo().log(
                action="broker_ticket_scope_mismatch",
                params={"expected_scope": scope, "actual_scope": row.get("scope"), "session_id": row.get("session_id")},
                result="denied",
                client_kind="broker",
            )
        except Exception:
            # Audit logging must never break the deny path itself.
            pass
        raise HTTPException(status_code=401, detail="ticket_scope_mismatch")


def _resolve_identity(session_id: str) -> tuple[str, str]:
    """Map a ticket's ``session_id`` (a chat session id) to the calling
    user's ``(id, email)`` for JWT minting.

    Goes through the existing chat-session lookup (``chat_session_repo()``,
    dual-backend) to get ``user_email``, then ``users_repo().get_by_email``
    (already dual-backend) — no new repository method required.
    """
    session = chat_session_repo().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="ticket_session_not_found")
    user = users_repo().get_by_email(session.user_email)
    if user is None:
        raise HTTPException(status_code=401, detail="ticket_user_not_found")
    return user["id"], user["email"]


async def _replay(request: Request, row: Dict[str, Any], body: Dict[str, Any]) -> httpx.Response:
    """Replay a ``{method, path, body}`` request in-process under a freshly
    minted session JWT for the ticket's resolved identity.

    Uses ``request.app`` (the exact FastAPI instance that received this
    broker call) for the ASGI transport, so the replay always targets the
    same app/config/DB the broker itself is running against — no reliance
    on a module-level app singleton.
    """
    path = str(body.get("path") or "")
    if path.startswith(_ADMIN_PATH_PREFIX):
        try:
            audit_repo().log(
                action="broker_admin_path_rejected",
                params={"path": path, "session_id": row.get("session_id")},
                result="denied",
                client_kind="broker",
            )
        except Exception:
            pass
        raise HTTPException(status_code=403, detail="admin_mutations_require_interactive_auth")

    user_id, email = _resolve_identity(row["session_id"])
    jwt_token = create_access_token(
        user_id=user_id,
        email=email,
        extra_claims={"chat_session_id": row["session_id"]},
    )
    method = str(body.get("method") or "GET").upper()
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://broker-replay") as client:
        return await client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {jwt_token}"},
            json=body.get("body"),
        )


def _to_response(resp: httpx.Response) -> Response:
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


@router.post("/agnes-api")
async def agnes_api(request: Request, row: Dict[str, Any] = Depends(require_broker_ticket)) -> Response:
    """Replay a main-CLI request under the ticket's resolved identity."""
    _require_scope(row, "main")
    body = await request.json()
    resp = await _replay(request, row, body)
    return _to_response(resp)


@router.post("/agnes-mcp")
async def agnes_mcp(request: Request, row: Dict[str, Any] = Depends(require_broker_ticket)) -> Response:
    """Replay an MCP-subprocess request under the ticket's resolved identity."""
    _require_scope(row, "mcp")
    body = await request.json()
    resp = await _replay(request, row, body)
    return _to_response(resp)


@router.post("/anthropic")
async def anthropic_proxy(request: Request, row: Dict[str, Any] = Depends(require_broker_ticket)) -> Response:
    """Inject the real Anthropic API key server-side and forward to the
    pinned Anthropic API — the sandbox's dummy key is discarded, and the
    target host is never taken from the agent-supplied request."""
    _require_scope(row, "main")
    raw_body = await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "authorization", "content-length", "x-api-key")
    }
    headers["x-api-key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    upstream_path = request.url.path[len("/api/broker/anthropic") :] or "/"
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            request.method,
            f"{_ANTHROPIC_BASE_URL}{upstream_path}",
            content=raw_body,
            headers=headers,
            params=request.query_params,
        )
    return _to_response(resp)
