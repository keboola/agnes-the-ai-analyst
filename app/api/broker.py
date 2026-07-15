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

    Fail-closed over ALL matching routes: if *any* route that matches this
    method+path is admin-gated, the whole request is treated as admin — never
    just the first match. This removes any dependence on route-registration
    order (a non-admin catch-all like ``/{full_path:path}`` registered before
    the real admin route must not shadow the gate) — the safe direction.
    """
    m = method.upper()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if m not in (route.methods or set()):
            continue
        if not route.path_regex.match(path):
            continue
        if require_admin in _dependant_calls(route.dependant):
            return True
    return False


def _normalize_broker_path(raw: Any) -> httpx.URL:
    """Canonicalize an agent-supplied replay path to the EXACT URL the ASGI
    dispatch will route on, pinned to the loopback authority, or 400.

    The admin-route gate and the ``httpx.ASGITransport`` dispatch must decide
    on the *same* path — otherwise a string that reads as non-admin to the
    gate but dispatches to an admin route bypasses the gate (RBAC review on
    #849 reproduced this end-to-end). ASGITransport routes on ``request.url.path``,
    which httpx produces by **percent-decoding and collapsing dot-segments** —
    so a literal check on the raw string diverges from what actually
    dispatches (``/api/sync/tri%67ger`` and ``/api/foo/../sync/trigger`` both
    resolve to ``/api/sync/trigger``).

    The fix: reject authority smuggling on the raw input (absolute URL,
    protocol-relative ``//host``, backslash, percent-encoded ``//``), then
    build the target ``httpx.URL`` ONCE against the pinned loopback host. The
    caller reads ``.path`` from this object for the gate **and** dispatches
    this very object — so the gate and the dispatch cannot diverge for any
    encoding. Query string is preserved; over-blocks at worst.
    """
    parsed = urlsplit(str(raw or ""))
    if parsed.scheme or parsed.netloc:
        raise HTTPException(status_code=400, detail="broker_path_must_be_local")
    path = parsed.path
    # Reject authority smuggling in both literal and percent-decoded forms
    # BEFORE canonicalizing (a leading `//` after decode is a protocol-relative
    # host; backslash is a `/` to some clients).
    for form in (path, unquote(path)):
        if not form.startswith("/") or form.startswith("//") or "\\" in form:
            raise HTTPException(status_code=400, detail="broker_path_must_be_local")
    reconstructed = f"{path}?{parsed.query}" if parsed.query else path
    target = httpx.URL("http://broker-replay" + reconstructed)
    # Dot-segment collapse can't produce a leading `//`, but re-validate the
    # canonical path defensively — it is what the gate and dispatch both use.
    if not target.path.startswith("/") or target.path.startswith("//"):
        raise HTTPException(status_code=400, detail="broker_path_must_be_local")
    return target


# Anthropic traffic is always forwarded to this pinned host — the sandbox's
# request never gets to choose where its "anthropic" call actually goes.
_ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# LLM completions routinely run for tens of seconds to minutes; httpx's 5s
# default read timeout makes EVERY real completion fail with httpx.ReadTimeout,
# leaving the sandbox agent with an empty response (chat looks "broken" even
# though isolation/auth are correct). Use a generous read timeout while keeping
# connect/write/pool bounded so a dead upstream still fails fast.
_ANTHROPIC_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)


def _add_anthropic_beta(headers: Dict[str, str], beta: str) -> None:
    """Ensure ``beta`` is present in the ``anthropic-beta`` header, appending to
    any value the in-sandbox SDK already set rather than overwriting it. The
    header lookup is case-insensitive (the SDK may send ``Anthropic-Beta``)."""
    for key in list(headers.keys()):
        if key.lower() == "anthropic-beta":
            existing = [v.strip() for v in headers[key].split(",") if v.strip()]
            if beta not in existing:
                existing.append(beta)
            headers[key] = ", ".join(existing)
            return
    headers["anthropic-beta"] = beta


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
    # scope="chat" is what makes `_stash_chat_session_id_from_token` stash the
    # chat_session_id that `execute_query`'s per-session BigQuery budget keys
    # off — it ignores the claim without that scope. The pre-broker solo token
    # (`mint_session_jwt`) carried it; keep it so the scan-budget cap still
    # applies to brokered solo sessions. (security review on #849)
    return create_access_token(
        user_id=user["id"],
        email=user["email"],
        extra_claims={"scope": "chat", "chat_session_id": session_id},
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

    # Canonicalize the agent-supplied path FIRST into the exact URL the ASGI
    # dispatch routes on, and read the gate's path from that same object — so
    # an absolute-URL / protocol-relative / percent-encoded / dot-segment path
    # can't defeat the gate while still hitting the real handler (RBAC review on
    # #849). A smuggling attempt is a probe → audit + 400.
    try:
        target = _normalize_broker_path(body.get("path"))
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

    # The gate decides on the SAME canonical path the dispatch will use
    # (``target.path`` is exactly ``request.url.path`` at replay time).
    match_path = target.path

    # Admin mutations are never brokered — refuse before touching identity,
    # regardless of whether the resolved identity is itself an admin. The
    # `/api/admin/` prefix is a fast-path; route introspection is the real
    # gate and catches admin routes at any path (§11).
    if match_path.startswith(_ADMIN_PATH_PREFIX) or _route_requires_admin(request.app, method, match_path):
        try:
            audit_repo().log(
                action="broker_admin_route_rejected",
                params={"path": match_path, "method": method, "session_id": row.get("session_id")},
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
            target,
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


@router.post("/anthropic", name="anthropic_proxy_bare")
@router.post("/anthropic/{subpath:path}", name="anthropic_proxy_subpath")
async def anthropic_proxy(request: Request, row: Dict[str, Any] = Depends(require_broker_ticket)) -> Response:
    """Inject the real Anthropic API key server-side and forward to the
    pinned Anthropic API — the sandbox's dummy key is discarded, and the
    target host is never taken from the agent-supplied request.

    Registered for both the bare path and any sub-path: the Anthropic SDK
    appends ``/v1/messages`` (etc.) to its base URL, so the real request
    arrives at ``/api/broker/anthropic/v1/messages``. The sub-path is
    recomputed from ``request.url.path`` and forwarded to the pinned host —
    the agent-supplied request still cannot choose the target host (Devin
    review on #849)."""
    _require_scope(row, "main")
    raw_body = await request.body()
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "authorization", "content-length", "x-api-key")
    }

    # Credential injection is the ONE thing that differs between auth modes; the
    # sandbox never carries either credential (it's added here, server-side).
    #   api_key (default)      → x-api-key: <static ANTHROPIC_API_KEY>
    #   workload_identity      → Authorization: Bearer <short-lived federated
    #                            token> + the oauth beta header OAuth-style
    #                            tokens require; NO static key exists.
    llm_auth = getattr(getattr(request.app.state, "chat_config", None), "llm_auth", "api_key")
    wif_mode = llm_auth == "workload_identity"
    if wif_mode:
        from app.auth.wif import WIFAuthError, get_federated_access_token

        try:
            token = get_federated_access_token()
        except WIFAuthError as exc:
            # Fail with a clear, non-leaking diagnostic rather than a 401 the
            # agent would surface as a confusing "API key is invalid".
            raise HTTPException(status_code=502, detail=f"workload_identity token exchange failed: {exc}") from exc
        headers["Authorization"] = f"Bearer {token}"
        _add_anthropic_beta(headers, "oauth-2025-04-20")
    else:
        headers["x-api-key"] = os.environ.get("ANTHROPIC_API_KEY", "")

    upstream_path = request.url.path[len("/api/broker/anthropic") :] or "/"
    async with httpx.AsyncClient(timeout=_ANTHROPIC_TIMEOUT) as client:
        resp = await client.request(
            request.method,
            f"{_ANTHROPIC_BASE_URL}{upstream_path}",
            content=raw_body,
            headers=headers,
            params=request.query_params,
        )
    # A 401 in WIF mode means the cached token was revoked before its declared
    # expiry — drop it so the next request re-mints.
    if wif_mode and resp.status_code == 401:
        from app.auth.wif import clear_token_cache

        clear_token_cache()
    return _to_response(resp)
