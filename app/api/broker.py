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
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute

from app.auth.access import mint_co_session_jwt, require_admin
from app.auth.jwt import create_access_token
from src.repositories import audit_repo, chat_session_repo, ticket_repo, users_repo

router = APIRouter(prefix="/api/broker", tags=["broker"])

# Admin mutations are never brokered — the broker replays only the
# interactive-parity surface (catalog/query/MCP), never admin writes,
# regardless of the resolved identity's own grants. The `/api/admin/` prefix
# is only a fast-path; the authoritative gate is `_route_requires_admin`,
# which catches every `Depends(require_admin)` route wherever it lives
# (e.g. `/api/users/*`, `/auth/admin/tokens/*`) — a bare path-prefix check
# missed those (Devin/agnes-review on #846, §11).
_ADMIN_PATH_PREFIX = "/api/admin/"


def _dependant_calls(dependant: Any) -> set:
    """Every dependency callable in a route's dependant tree (recursive)."""
    calls: set = set()
    stack = [dependant]
    while stack:
        d = stack.pop()
        call = getattr(d, "call", None)
        if call is not None:
            calls.add(call)
        stack.extend(getattr(d, "dependencies", None) or [])
    return calls


def _route_requires_admin(app: Any, method: str, path: str) -> bool:
    """True if the concrete ``method`` + ``path`` resolves to an app route
    gated by ``Depends(require_admin)``.

    Introspects the real route table (not a path prefix), so it catches admin
    mutations at any path — the exact gap the prefix-only check left open.
    A path that matches no route returns False (the replay will 404 harmlessly).
    """
    m = method.upper()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if m not in (route.methods or set()):
            continue
        if route.path_regex.match(path):
            return require_admin in _dependant_calls(route.dependant)
    return False


def _normalize_broker_path(raw: Any) -> str:
    """Canonicalize an agent-supplied replay path to a server-LOCAL absolute
    path, or 400.

    The same string is used for BOTH the admin-route gate and the
    ``httpx.ASGITransport`` replay dispatch — they must never diverge.
    ASGITransport routes purely on the URL path component and ignores any
    scheme/authority, so a smuggled absolute URL (``http://x/api/…``) or a
    protocol-relative path (``//x/api/…``) slips past a literal admin check
    yet still executes the real handler (RBAC review on #849 reproduced this
    end-to-end). Reject anything that isn't a plain server-local absolute
    path; the query string is preserved. Over-blocks at worst — the safe
    direction. Also rejects backslash and percent-encoded ``//`` smuggling
    (``%2f%2f`` decodes to a leading ``//``).
    """
    parsed = urlsplit(str(raw or ""))
    if parsed.scheme or parsed.netloc:
        raise HTTPException(status_code=400, detail="broker_path_must_be_local")
    path = parsed.path
    # Check both the literal and percent-decoded forms: the router the replay
    # ultimately hits decodes the path, so a check on the raw form alone could
    # diverge from what actually dispatches.
    for form in (path, unquote(path)):
        if not form.startswith("/") or form.startswith("//") or "\\" in form:
            raise HTTPException(status_code=400, detail="broker_path_must_be_local")
    return f"{path}?{parsed.query}" if parsed.query else path


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


def _mint_identity_jwt(session_id: str) -> str:
    """Mint the JWT the replayed request runs under, for the ticket's session.

    - **Co-session**: mint a ``co_session`` JWT (``mint_co_session_jwt``). It
      carries a synthetic ``sub`` and NO baked-in identity; the downstream
      auth path recomputes the participant grant-intersection **live, per
      request** (``compute_grant_intersection`` over ``chat_session_participants``
      with ``left_at IS NULL``). Resolving a co-session to its single stored
      owner (the previous behaviour) both over-authorized guests and went
      stale when the owner left — see §11.
    - **Solo session**: resolve the owner via the dual-backend chat-session +
      users lookup and mint an ordinary identity JWT.

    Both carry ``chat_session_id`` so ``execute_query``'s per-session BigQuery
    budget accounting works identically to a direct call.
    """
    session = chat_session_repo().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="ticket_session_not_found")
    if getattr(session, "is_co_session", False):
        return mint_co_session_jwt(session_id)
    user = users_repo().get_by_email(session.user_email)
    if user is None:
        raise HTTPException(status_code=401, detail="ticket_user_not_found")
    return create_access_token(
        user_id=user["id"],
        email=user["email"],
        extra_claims={"chat_session_id": session_id},
    )


async def _replay(request: Request, row: Dict[str, Any], body: Dict[str, Any]) -> httpx.Response:
    """Replay a ``{method, path, body}`` request in-process under a freshly
    minted session JWT for the ticket's resolved identity.

    Uses ``request.app`` (the exact FastAPI instance that received this
    broker call) for the ASGI transport, so the replay always targets the
    same app/config/DB the broker itself is running against — no reliance
    on a module-level app singleton.
    """
    method = str(body.get("method") or "GET").upper()

    # Canonicalize the agent-supplied path FIRST and use the same string for
    # the admin gate and the dispatch below — an absolute-URL / protocol-relative
    # path would otherwise defeat the gate while still hitting the real handler
    # (RBAC review on #849). A smuggling attempt is a probe → audit + 400.
    try:
        path = _normalize_broker_path(body.get("path"))
    except HTTPException:
        try:
            audit_repo().log(
                action="broker_path_rejected",
                params={"raw_path": str(body.get("path"))[:200], "session_id": row.get("session_id")},
                result="denied",
                client_kind="broker",
            )
        except Exception:
            pass
        raise

    match_path = path.split("?", 1)[0]

    # Admin mutations are never brokered — refuse before touching identity,
    # regardless of whether the resolved identity is itself an admin. The
    # `/api/admin/` prefix is a fast-path; route introspection is the real
    # gate and catches admin routes at any path (§11).
    if match_path.startswith(_ADMIN_PATH_PREFIX) or _route_requires_admin(request.app, method, match_path):
        try:
            audit_repo().log(
                action="broker_admin_route_rejected",
                params={"path": path, "method": method, "session_id": row.get("session_id")},
                result="denied",
                client_kind="broker",
            )
        except Exception:
            pass
        raise HTTPException(status_code=403, detail="admin_mutations_require_interactive_auth")

    jwt_token = _mint_identity_jwt(row["session_id"])
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
