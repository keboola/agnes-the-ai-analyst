"""Host-side wiring for an embedded ``kai-agent`` turn engine.

``kai-agent`` (keboola/ui ``apps/kai-agent``) is a Claude-Agent-SDK turn engine
that embeds in a host platform through a ``HostModule`` port. Its ``jwt`` host
adapter expects the host to supply exactly three things; this module is all
three:

- **A session token.** ``POST /api/kai/sessions`` mints the short-lived HS256
  JWT the engine verifies on every call. Its claims *are* the principal the
  engine runs under (``sub`` → user, ``tenant`` → deployment, ``scope_id`` →
  this chat session), plus a ``downstream_credential`` the engine hands back
  to us when it needs per-turn tickets. Agnes owns the chat-session id, so the
  same call creates the ``chat_sessions`` row and returns its id — the caller
  passes it to the engine as ``body.id`` and both sides key off it (no
  cross-database join).
- **A ticket endpoint.** ``POST /api/kai/tickets`` mints one short-lived,
  scope-split broker ticket per egress scope the engine's in-sandbox relay
  needs, once per turn. This is the same ``chat_broker_tickets`` machinery the
  native chat relay uses (``src/repositories/ticket.py``) — the engine's
  ``llm`` scope maps onto our ``main`` ticket scope, its ``mcp`` scope onto
  ``mcp``.
- **A broker.** Already built: ``/api/broker/anthropic/{subpath}``
  (``app/api/broker.py``) is a plain pass-through that authenticates a
  ``main``-scoped ticket over ``Authorization: Bearer`` and injects the real
  upstream credential server-side. The engine's relay speaks exactly that
  shape, so no new LLM route is needed — point ``HOST_BROKER_LLM_URL`` at it.

The security posture is inherited, not re-invented: the E2B sandbox holds no
credential, only a per-turn ticket, and every LLM byte transits our broker
where it is already authorized, model-gated, budgeted and metered.

Deployment config (all env, like the rest of the broker's upstream wiring):

- ``KAI_HOST_JWT_SECRET`` — HS256 secret shared with the engine's
  ``HOST_JWT_SECRET``. **Unset disables this integration** (every route 503s),
  so an instance that does not embed the engine exposes nothing.
- ``KAI_HOST_JWT_ISSUER`` / ``KAI_HOST_JWT_AUDIENCE`` — must equal the
  engine's ``HOST_JWT_ISSUER`` / ``HOST_JWT_AUDIENCE``; the engine rejects a
  mismatch as an unauthenticated token.
- ``KAI_TENANT_ID`` — the ``tenant`` claim, i.e. the engine's ``projectId``
  tenant key. One value per deployment; every chat row the engine writes is
  scoped by it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from base64 import urlsafe_b64encode
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.chat.types import Surface
from src.repositories import audit_repo, chat_session_repo, ticket_repo

router = APIRouter(prefix="/api/kai", tags=["kai"])

#: Lifetime of a Kai session token *and* of the credential it carries. The
#: engine hard-caps ``exp`` at 24 h and rejects anything longer (there is no
#: revocation on its verify path, so the cap bounds a leaked token's damage
#: window). 12 h keeps a working day inside one token while staying well under
#: that ceiling.
_SESSION_TTL_SECONDS = 12 * 60 * 60

#: Ticket lifetime handed to the engine's relay. Deliberately short: the engine
#: re-mints on every turn, so this only has to outlive a single turn's upstream
#: calls. It must still cover a turn parked on an interactive tool, which is
#: why it is an hour rather than minutes.
_TICKET_TTL_SECONDS = 60 * 60

#: Ticket scope for the credential the engine stores in its session JWT. It is
#: NOT a broker ticket: presenting it to any ``/api/broker/*`` route fails
#: those routes' own ``_require_scope`` check. Its only power is to mint the
#: per-turn tickets below, which is what keeps a long-lived credential out of
#: reach of the sandbox — the sandbox sees per-turn tickets only.
_CREDENTIAL_SCOPE = "kai_session"

#: The engine's egress scopes (core-owned wire names in ``kai-agent``) mapped
#: onto our broker's ticket scopes. ``llm`` is mandatory — the engine rejects a
#: ticket payload without it and fails the turn before any prompt reaches the
#: sandbox.
_EGRESS_SCOPES: Dict[str, str] = {"llm": "main", "mcp": "mcp"}


def _secret() -> str:
    """The shared HS256 secret, or 503 when this deployment does not embed the
    engine. ``strip()`` guards against a trailing newline from a secret
    manager — an invisible ``\\n`` here is an opaque 401 at the engine."""
    secret = os.environ.get("KAI_HOST_JWT_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="kai_integration_not_configured")
    return secret


def _issuer() -> str:
    return os.environ.get("KAI_HOST_JWT_ISSUER", "agnes").strip() or "agnes"


def _audience() -> str:
    return os.environ.get("KAI_HOST_JWT_AUDIENCE", "kai-agent").strip() or "kai-agent"


def _tenant_id() -> str:
    return os.environ.get("KAI_TENANT_ID", "agnes").strip() or "agnes"


def _b64url(raw: bytes) -> str:
    """base64url without padding, per RFC 7515 §2."""
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign_session_jwt(*, claims: Dict[str, Any], secret: str) -> str:
    """Sign ``claims`` as a compact HS256 JWT.

    Hand-rolled rather than pulling a JWT library into this path on purpose:
    ``app/auth/jwt.py`` signs Agnes's *own* session tokens with Agnes's own
    key and claim set, and this token is neither — it is the engine's
    credential, signed with the engine's shared secret, carrying the engine's
    claim vocabulary. Reusing that module would couple two independently
    rotating secrets. HS256 over a JSON pair is ~10 lines and has no
    negotiable parameters (no ``alg`` confusion surface: the algorithm is
    fixed here and fixed at the verifier).

    ``separators`` pins compact JSON so the signed bytes are exactly the bytes
    transmitted.
    """
    segments = [
        _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()),
        _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    segments.append(_b64url(signature))
    return ".".join(segments)


class KaiSessionResponse(BaseModel):
    """What a client needs to start talking to the engine: the token to send
    as ``Authorization: Bearer``, and the chat id to send as ``body.id`` so
    the engine's chat row and our ``chat_sessions`` row share a key."""

    chat_id: str
    token: str
    expires_at: int


def _create_session_and_credential(user_email: str) -> tuple[str, str]:
    """The synchronous DB half of session minting: the ``chat_sessions`` row
    plus the credential bound to it. Split out so the route can offload it off
    the event loop in one hop.

    The id is a **UUID**, not the repo's default ``chat_<hex>``. The engine
    accepts a client-supplied ``body.id`` — that is what lets both sides key
    off one value with no cross-database join — but it stores it in a Postgres
    ``uuid`` column and validates the shape on the way in, so a ``chat_<hex>``
    id is rejected with ``Invalid UUID`` before the turn starts. Agnes's own
    column is a VARCHAR, so a UUID sits in it unchanged.
    """
    session = chat_session_repo().create_session(
        user_email=user_email,
        surface=Surface.WEB,
        session_id=str(uuid.uuid4()),
    )
    # The credential is minted against the session, so resolving it later
    # yields the identity + session without the engine ever sending either.
    credential = ticket_repo().mint(session.id, _CREDENTIAL_SCOPE, ttl_seconds=_SESSION_TTL_SECONDS)
    return session.id, credential


@router.post("/sessions", response_model=KaiSessionResponse)
async def create_kai_session(user: dict = Depends(get_current_user)) -> KaiSessionResponse:
    """Create a chat session and mint the engine session token for it.

    Authenticated as an ordinary user: the caller can only ever mint a token
    for **themselves**, because every identity claim is taken from the
    resolved ``user`` and none from the request body. There is no request body
    at all — that is the point. A body-supplied ``sub`` would make this an
    impersonation endpoint.

    ``async def`` + ``to_thread`` rather than a plain ``def``: the repo calls
    are synchronous DuckDB/PG work that must not run on the single uvicorn
    event loop (Tier 1, PR #188), and a sync route body would also run inside
    the threadpool where the dev debug-toolbar's profiler cannot stop itself
    ("Failed to stop profiling … same thread" → 500 in local dev). Every other
    route in this family is async for the same reason.
    """
    secret = _secret()
    session_id, credential = await asyncio.to_thread(_create_session_and_credential, user["email"])
    now = int(time.time())
    expires_at = now + _SESSION_TTL_SECONDS
    token = _sign_session_jwt(
        claims={
            "sub": user["email"],
            "tenant": _tenant_id(),
            "scope_id": session_id,
            "downstream_credential": credential,
            # The engine reads this as its `isReadOnly` turn gate. Agnes has
            # no read-only chat mode today, so it is always a read-write
            # session; stated explicitly rather than relying on the engine's
            # default so the claim set is complete on the wire.
            "read_only": False,
            "iss": _issuer(),
            "aud": _audience(),
            "iat": now,
            "exp": expires_at,
        },
        secret=secret,
    )
    return KaiSessionResponse(chat_id=session_id, token=token, expires_at=expires_at)


def _require_session_credential(request: Request) -> Dict[str, Any]:
    """Resolve the session credential the engine presents, or 401.

    Plain ``def`` (not ``async def``) so FastAPI offloads it to the anyio
    thread pool — the body does a synchronous ``ticket_repo().resolve`` DB
    read that must not run on the single uvicorn event loop, matching
    ``app/api/broker.py``'s ``require_broker_ticket``.

    Scope is checked here rather than trusting the caller: a *broker* ticket
    (``main``/``mcp``) presented to this route must not be able to mint more
    tickets, or a sandbox that got hold of one turn's ticket could refresh
    itself indefinitely.
    """
    auth = request.headers.get("authorization", "")
    credential = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not credential:
        raise HTTPException(status_code=401, detail="missing_kai_credential")
    row = ticket_repo().resolve(credential)
    if row is None:
        raise HTTPException(status_code=401, detail="invalid_or_expired_kai_credential")
    if row.get("scope") != _CREDENTIAL_SCOPE:
        try:
            audit_repo().log(
                action="kai_credential_scope_mismatch",
                params={"actual_scope": row.get("scope"), "session_id": row.get("session_id")},
                result="denied",
                client_kind="broker",
            )
        except Exception:
            # Audit logging must never break the deny path itself.
            pass
        raise HTTPException(status_code=401, detail="kai_credential_scope_mismatch")
    return row


def _rotate_egress_tickets(session_id: str, scopes: Dict[str, str]) -> Dict[str, str]:
    """The synchronous DB half of ticket minting. One offload hop for the whole
    revoke-then-mint sequence, which must also stay contiguous: interleaving
    another turn's mint between them would hand out a ticket and immediately
    retire it."""
    # Scope-limited on purpose. `revoke_session` would also delete the
    # long-lived session credential the request authenticated with, and the
    # engine has no way to be handed a replacement: its ticket-response schema
    # is `{llm, mcp}` and it keeps using the credential baked into the session
    # JWT. A scope-blind sweep here 401s every subsequent turn.
    ticket_repo().revoke_session_scopes(session_id, list(_EGRESS_SCOPES.values()))
    return {
        egress_scope: ticket_repo().mint(session_id, broker_scope, ttl_seconds=_TICKET_TTL_SECONDS)
        for egress_scope, broker_scope in scopes.items()
    }


@router.post("/tickets")
async def issue_kai_tickets(row: Dict[str, Any] = Depends(_require_session_credential)) -> Dict[str, str]:
    """Mint this turn's scope-split broker tickets for the engine's relay.

    Called once per turn by the engine's server (never by the sandbox), which
    pushes the result into the sandbox over stdin. The engine requires ``llm``
    and treats every other key as optional host data, so an instance that
    brokers no MCP upstream simply omits it — here that means
    ``KAI_BROKER_MCP_ENABLED`` unset.

    Minting retires the session's previous egress tickets, so one live set
    exists per chat and a stale turn's ticket cannot be replayed — mirroring
    the engine's own contract ("minting retires the chat's previous set").
    """
    scopes = dict(_EGRESS_SCOPES)
    if not os.environ.get("KAI_BROKER_MCP_ENABLED", "").strip():
        scopes.pop("mcp", None)
    return await asyncio.to_thread(_rotate_egress_tickets, row["session_id"], scopes)
