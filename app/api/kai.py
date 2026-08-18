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
  ``llm`` scope maps onto a broker scope of the same name and its ``mcp`` scope
  onto ``mcp``. Deliberately NOT the native sandbox's ``main``: that scope also
  authenticates ``/api/broker/agnes-api``, so reusing it would hand the engine's
  sandbox the caller's whole non-admin ``/api/*`` replay surface rather than LLM
  egress. ``anthropic_proxy`` accepts both.
- **Brokers.** The LLM one was already built: ``/api/broker/anthropic/{subpath}``
  (``app/api/broker.py``) is a plain pass-through that authenticates a
  ``main``-scoped ticket over ``Authorization: Bearer`` and injects the real
  upstream credential server-side. The engine's relay speaks exactly that
  shape, so no new LLM route is needed — point ``HOST_BROKER_LLM_URL`` at it.
  ``POST /api/kai/mcp`` is the tool equivalent: it forwards the sandbox's
  verbatim Streamable-HTTP request to Agnes's own MCP server under the
  ticket's real identity.

Optionally, a fourth: ``GET /api/kai/workspace`` serves the caller's workspace
tree as one gzipped tarball, which the engine materializes into its sandbox's
project scope. That is what gives the embedded engine Agnes's CLAUDE.md, org
safety hook and bundled skills instead of a bare Claude Code.

The security posture is inherited, not re-invented: the E2B sandbox holds no
credential, only a per-turn ticket, and every LLM and MCP byte transits our
broker where it is already authorized, model-gated, budgeted and metered.

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
- ``KAI_BROKER_MCP_ENABLED`` — issue the ``mcp`` ticket scope, i.e. let the
  engine's sandbox reach ``/api/kai/mcp``. Unset means the engine registers no
  host MCP server and the agent runs with its built-in tools only.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import io
import json
import logging
import os
import tarfile
import threading
import time
import uuid
from base64 import urlsafe_b64encode
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.broker import _require_scope, require_broker_ticket
from app.auth.dependencies import get_current_user, reject_keboola_header_credential
from app.chat.types import Surface
from src.repositories import audit_repo, chat_session_repo, ticket_repo

logger = logging.getLogger(__name__)

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
#: The engine's egress scope names (its ticket-response keys) mapped onto the
#: broker scopes that authenticate them. `llm` maps to the broker's own `llm`
#: scope, NOT to `main`: `main` authenticates `/api/broker/agnes-api` as well
#: as the LLM proxy, so minting the engine's LLM ticket as `main` handed the
#: sandbox the caller's whole non-admin `/api/*` replay surface — reachable
#: even with the tool switch off, which is not what "LLM egress" means.
#:
#: `mcp` needed the identical treatment and at first did not get it, which left
#: the confinement half-done: `/api/broker/agnes-mcp` is gated on the native
#: relay's `mcp` scope and hands off to the same `_replay`, whose own gate
#: "only blocks admin-mutation routes" (`app/api/broker.py`) — so a ticket
#: minted as plain `mcp` opened the very general `/api/*` surface the `llm`
#: split was made to withhold. The engine's tool ticket is therefore minted as
#: `kai_mcp`, a scope only `/api/kai/mcp` accepts. Two consequences beyond the
#: confinement: the engine's ticket can no longer reach `agnes-mcp`, and a
#: NATIVE chat sandbox's `mcp` ticket can no longer reach `/api/kai/mcp` —
#: which is what made that route reachable from sessions it was never designed
#: for in the first place. Both halves found by Devin Review on this PR.
_EGRESS_SCOPES: Dict[str, str] = {"llm": "llm", "mcp": "kai_mcp"}


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


def _broker_mcp_enabled() -> bool:
    """Whether this instance issues the ``mcp`` ticket scope.

    Routed through :func:`app.instance_config.feature_enabled` rather than a
    bare ``os.environ`` read, because a switch is the one place where "any
    non-blank value is on" is actively harmful: an operator who writes
    ``KAI_BROKER_MCP_ENABLED=false`` to take the engine's tool surface away
    would have handed it to them instead. `feature_enabled` applies the
    shared truthy convention (``0`` / ``false`` / ``no`` / ``off`` / empty are
    off, per ``docs/feature-flags.md``) and lets the same switch be set in
    ``instance.yaml`` under ``kai.broker_mcp_enabled`` for deployments that
    configure Agnes there rather than through the environment.

    Unlike the other ``KAI_*`` names this one is a toggle, not a credential or
    an identity claim — which is why it, alone among them, is registered in
    ``app.switches.SWITCHES``.
    """
    from app.instance_config import feature_enabled

    return feature_enabled("kai", "broker_mcp_enabled", env_var="KAI_BROKER_MCP_ENABLED", default=False)


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

    **Known consequence of ``Surface.WEB``:** this is an ordinary chat row, so
    it appears in the caller's chat history — and while the engine holds the
    transcript in its own database, it renders there as an empty, untitled
    conversation. The row itself is deliberate, not incidental: it is the RBAC
    anchor, the GDPR purge target and the join key, so it has to exist. Only
    the *surface value* is a judgement call, and `WEB` is kept because that is
    what these sessions are from the user's side; `list_sessions` filters on
    email + archived and not on surface, so no other value would hide them
    either. Making them presentable is a frontend decision (render from the
    engine, or filter engine-backed rows out of the list) rather than
    something to paper over here.

    **What sharing the row costs, and the half that is still open.** The row
    also shares its `chat_broker_tickets` namespace with a native sandbox on
    the same id. A user opening this conversation in web chat spawns a runner
    whose lifecycle sweep used to delete every ticket for the id — including
    the engine's long-lived credential, killing the session for good with no
    channel to hand it a replacement. That half is closed:
    `SWEEP_EXEMPT_SCOPES` (`src/repositories/ticket.py`) keeps
    `revoke_session` off long-lived credentials, in both backends.

    The converse — an engine turn stripping a live native sandbox's egress
    tickets — was real while both sides minted the same broker scopes, and is
    now closed by construction rather than by policy: a turn touches
    `{llm, kai_mcp}` (`_EGRESS_SCOPES` values) and a native web-chat sandbox
    holds `{main, mcp, data_apps}` (`app/chat/manager.py`). The sets are
    disjoint with the tool switch on or off, so neither runtime's rotation can
    reach the other's tickets. Nothing here revokes the literal `mcp` scope;
    the only `"mcp"` left in this module is the engine-facing dict KEY.

    That disjointness is a side effect of confining the tool ticket to
    `kai_mcp`, not an independent guarantee — reusing a native scope here would
    silently restore the collision, which is why `_EGRESS_SCOPES` says so at its
    definition. What remains is narrower than a ticket collision: the row is
    still shared, so it is still an ordinary conversation in the caller's
    history, and a separate id space with its own RBAC anchor and purge target
    is still the cleaner shape. Found by Devin Review; the collision's
    disappearance verified on-branch by ZdenekSrotyr, whose reading was right
    where an earlier version of this note (and my reply defending it) was not.
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


#: Both credential-returning routes answer with `no-store`: their bodies are a
#: live session JWT and live broker tickets, and an intermediary or browser
#: cache holding either is a credential at rest in a place nobody audits.
#: `no-store` (not merely `no-cache`) is the directive that forbids writing it
#: down at all. Found by Copilot on this PR.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


@router.post(
    "/sessions",
    response_model=KaiSessionResponse,
    # Same guard every other credential-minting surface carries
    # (`app/api/data_apps.py`, `app/api/cowork_bundle.py`): an
    # `X-StorageApi-Token` header credential must not be exchangeable for a
    # durable follow-on credential. This route mints two — the engine's session
    # JWT and the `kai_session` broker credential behind it, which then mints
    # egress tickets and spends the instance's LLM budget — so it belongs in
    # that set, and `keboola_token_header`'s own description already promises
    # "credential-minting endpoints stay blocked". Route-level so the handler's
    # `Depends(get_current_user)` still populates `user` (FastAPI dedupes the
    # two calls). Found by Devin Review on this PR.
    dependencies=[Depends(reject_keboola_header_credential)],
)
async def create_kai_session(response: Response, user: dict = Depends(get_current_user)) -> KaiSessionResponse:
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
    response.headers.update(_NO_STORE)
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
    # The kill switch is checked on EVERY route, not just session creation.
    # Otherwise unsetting it stops new sessions while every already-issued
    # credential keeps minting egress tickets and reaching tools for the rest
    # of its 12 h life — an operator who disables the integration expects it to
    # stop now, and there is no revocation on the engine's own verify path.
    _secret()
    auth = request.headers.get("authorization", "")
    credential = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not credential:
        raise HTTPException(status_code=401, detail="missing_kai_credential")
    row = ticket_repo().resolve(credential)
    if row is None:
        raise HTTPException(status_code=401, detail="invalid_or_expired_kai_credential")
    # The credential's authority is bounded by its session ROW, not only by its
    # own TTL. `SWEEP_EXEMPT_SCOPES` deliberately spares it from the
    # sandbox-lifecycle sweep, and `kill()` — which runs that sweep — is also
    # what a user's permanent delete reaches, so without this check a deleted
    # conversation left the engine able to mint fresh upstream tickets and
    # spend the instance's LLM budget under that user's name for the rest of
    # the credential's 12 h life. Checked here rather than per route so
    # `/tickets` and `/workspace` are both covered by construction. Found by
    # Devin Review on this PR.
    if chat_session_repo().get_session(row["session_id"]) is None:
        raise HTTPException(status_code=401, detail="kai_session_gone")
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
    """The synchronous DB half of ticket minting — one offload hop for the
    whole revoke-then-mint sequence.

    One hop keeps the two statements adjacent *within this call*; it does NOT
    serialize concurrent calls, and an earlier version of this docstring
    claimed otherwise. ``asyncio.to_thread`` hands the body to a worker
    thread, so two rotations for the same chat can still interleave as
    revoke(A) → mint(A) → revoke(B) → mint(B), leaving A holding a ticket that
    was retired a moment after it was issued. Not reachable through the engine
    as it stands — its host contract is one rotation per turn per chat, and a
    turn's tickets are minted before the turn starts — so this is a documented
    limit rather than a live bug. If parallel turns per chat ever become
    possible the fix is a per-session lock, or folding revoke+mint into one
    statement that retires only rows older than the new mint; a second offload
    hop would not help. Found by Devin Review on this PR."""
    # Scope-limited on purpose. `revoke_session` would also delete the
    # long-lived session credential the request authenticated with, and the
    # engine has no way to be handed a replacement: its ticket-response schema
    # is `{llm, mcp}` and it keeps using the credential baked into the session
    # JWT. A scope-blind sweep here 401s every subsequent turn.
    # Revoke exactly what this turn re-mints, not every scope the map knows.
    # With the tool switch off `scopes` has no `mcp` key, so sweeping the whole
    # map retired a ticket nothing was going to replace — pointless work, and
    # the reason to align the two. (An earlier version of this comment also
    # claimed the sweep deleted a concurrent web-chat sandbox's MCP ticket.
    # That was true when the engine minted the native `mcp` scope; confining
    # the tool ticket to `kai_mcp` in this same PR made the two sets disjoint,
    # so the collision was already gone by the time this was written — noted by
    # ZdenekSrotyr.) A previously-issued `kai_mcp` ticket is not proactively
    # retired here, which costs nothing: short TTL, and `/api/kai/mcp` refuses
    # outright while the switch is off. Found by Copilot on this PR.
    ticket_repo().revoke_session_scopes(session_id, list(scopes.values()))
    return {
        egress_scope: ticket_repo().mint(session_id, broker_scope, ttl_seconds=_TICKET_TTL_SECONDS)
        for egress_scope, broker_scope in scopes.items()
    }


@router.post("/tickets")
async def issue_kai_tickets(
    response: Response, row: Dict[str, Any] = Depends(_require_session_credential)
) -> Dict[str, str]:
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
    if not _broker_mcp_enabled():
        scopes.pop("mcp", None)
    response.headers.update(_NO_STORE)
    return await asyncio.to_thread(_rotate_egress_tickets, row["session_id"], scopes)


# ---------------------------------------------------------------------------
# MCP passthrough
#
# The engine's in-sandbox relay speaks plain pass-through: it forwards the
# SDK's verbatim Streamable-HTTP request to whatever URL the host put in the
# `mcp` scope, carrying only that turn's ticket. So this route is a proxy, not
# a replay envelope like `/api/broker/agnes-mcp` — there is no
# `{method, path, body}` to describe, just bytes to forward.
# ---------------------------------------------------------------------------

#: Where the brokered request is dispatched: the mounted Streamable-HTTP MCP
#: app — the same surface, tools and RBAC a Claude Desktop connector reaches;
#: the broker adds no authority of its own.
_MCP_STREAMABLE_PATH = "/api/mcp/http/"


def _mcp_internal_base() -> str:
    """Base URL for the self-call that reaches the mounted MCP app.

    A **real** HTTP hop, not `httpx.ASGITransport`. The transport looks like it
    streams — you can pass `stream=True` — but it runs the whole ASGI app to
    completion, accumulating every `http.response.body` chunk in a list, and
    only then hands back a stream that yields one joined blob. Two consequences
    that both matter here:

    - Nothing reaches the sandbox until the tool has finished. Worse than slow:
      the engine's relay bounds *time to headers* (~15 s), and buffering means
      headers cannot arrive before the tool completes — so every MCP tool
      slower than that bound would die on a relay-level 502 rather than
      returning late.
    - `httpx.Timeout` is inert, because there is no network layer to apply it
      to. A hung tool would hold the request open indefinitely.

    Same env var and default the MCP tools already use for their own self-calls
    (`app/api/mcp_streamable.py`, `app/api/mcp_http.py`), so a deployment that
    has MCP working at all already has this pointing at the right place — and
    if it does not, MCP tools are broken independently of this route.
    """
    return os.environ.get("AGNES_MCP_INTERNAL_URL", "http://localhost:8000").rstrip("/")


#: Bounds on the brokered MCP hop. `read` is generous because a tool may think
#: for a while before writing; `connect` is tight because the target is this
#: same process. Unlike the ASGI transport these are real: the hop is HTTP.
_MCP_PROXY_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)

#: TTL of the access token minted for the brokered identity. Short, because it
#: exists only to carry one chat's MCP traffic and is re-minted on demand.
_MCP_ACCESS_TOKEN_TTL_SECONDS = 15 * 60

#: Re-mint this long before expiry so a token cannot lapse mid-request.
_MCP_ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 60

#: Client id recorded on the minted access token. Not a registered OAuth
#: client — nothing in the verify path requires one — but it labels the rows
#: so an operator reading `oauth_access_tokens` can tell brokered engine
#: traffic from a real connector.
_MCP_CLIENT_ID = "kai-agent-broker"

#: Scopes the brokered token carries. Must be exactly the MCP app's own
#: vocabulary — `AuthSettings(required_scopes=["read"])` in
#: ``app/api/mcp_streamable.py``, which is also the only scope its client
#: registration issues. Anything else authenticates and then fails the
#: middleware's scope check with `403 insufficient_scope`. This is an OAuth
#: scope, not an authorization decision: RBAC is enforced downstream from the
#: resolved identity, exactly as it is for a real connector.
_MCP_SCOPES = ["read"]

#: session_id -> (token, expires_at). Minting is a DB write, and an MCP turn
#: makes many JSON-RPC calls, so the token is reused across them. In-process
#: like the broker's own budget cache — Agnes runs a single chat worker, and a
#: lost cache just re-mints.
#:
#: Swept on every mint (see `_prune_mcp_token_cache`). Without that it is an
#: unbounded leak rather than a cache: a chat's entry is never read again once
#: the chat ends, but it would hold a full JWT for the process lifetime, so
#: memory grew with the number of engine chats ever served and never shrank.
_mcp_token_cache: Dict[str, tuple[str, int]] = {}

#: Guards every read, prune and write of `_mcp_token_cache`. A `threading.Lock`
#: rather than an asyncio one because the function that touches the cache runs
#: on the anyio worker pool, not the event loop. Held only around the dict
#: operations — never across the DB read or the JWT signing — so mints stay
#: concurrent; two threads racing to mint for one session simply both win, and
#: the last write stands (both tokens are valid and registered).
_mcp_token_cache_lock = threading.Lock()

#: Headers never copied from the sandbox's request. `authorization` is
#: replaced with the minted identity; the rest are hop-by-hop or recomputed by
#: httpx. Mirrors the relay's own drop list.
_MCP_DROP_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailer",
        "proxy-authorization",
        "proxy-connection",
        "accept-encoding",
        "expect",
        "cookie",
        "x-api-key",
        # Forwarding headers, dropped so the sandbox cannot forge the client IP
        # on the internal hop. `trusted_client_ip` (app/auth/client_ip.py) reads
        # `x-forwarded-for` and trusts the RIGHTMOST
        # `AGNES_TRUSTED_PROXY_HOPS` entries; a value the sandbox supplied would
        # arrive as an extra hop, landing an attacker-chosen address in the
        # audit log and in any IP-keyed throttle on this call. The security
        # playbook's rule is to derive the IP from trusted proxy hops only, and
        # forwarding an agent-supplied one defeats the hop arithmetic that rule
        # rests on. Found by Copilot on this PR.
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-forwarded-port",
        "forwarded",
        "x-real-ip",
    }
)

#: Response headers httpx recomputes or that must not cross back into the
#: sandbox. `mcp-session-id` deliberately DOES pass through — the MCP session
#: dies without it.
_MCP_DROP_RESPONSE_HEADERS = frozenset(
    {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive", "set-cookie"}
)


def _prune_mcp_token_cache(now: int) -> None:
    """Drop entries whose token has expired.

    An entry is dead weight the moment its token expires — the next mint for
    that session replaces it anyway — and a chat that never comes back would
    otherwise keep a JWT resident for the process lifetime. Sweeping the whole
    dict is fine at this size: one entry per live engine chat, and the sweep
    runs only on a mint (a DB write already dominates it).
    """
    # Snapshot then `pop(..., None)`, and every caller holds
    # `_mcp_token_cache_lock`. `_mint_mcp_access_token` runs under
    # `asyncio.to_thread`, so several worker threads reach this dict at once:
    # iterating it live raised `RuntimeError: dictionary changed size during
    # iteration`, and a bare `del` raced another thread's prune into a
    # `KeyError` — both surfacing as intermittent 500s on `/api/kai/mcp` rather
    # than as anything diagnosable. Found by Copilot on this PR.
    for stale in [sid for sid, (_, exp) in list(_mcp_token_cache.items()) if exp <= now]:
        _mcp_token_cache.pop(stale, None)


def _mint_mcp_access_token(session_id: str) -> str:
    """An access token the mounted MCP app's verifier accepts, for the
    identity behind ``session_id``.

    The verifier resolves a bearer against ``oauth_access_tokens`` — a plain
    session JWT is not enough — so this mints the same shape the OAuth code
    exchange does and registers it: an Agnes session JWT carrying
    ``scope='mcp-oauth'``, saved with its digest. That scope is load-bearing,
    not decoration: ``resolve_token_to_user`` reads it to stamp the stack
    data-read surface, which is the correct posture for an agent surface —
    the engine follows its user's stack rather than inheriting an admin's
    catalog god-mode.

    **The two narrowed session kinds are refused, not resolved to the owner.**
    ``_mint_identity_jwt`` (the native replay path) answers a co-session with a
    live participant grant-intersection and a scope-limited agent session with
    owner-grants ∩ agent-scope, and it documents the fall-through to the stored
    owner as the bug that "over-authorized guests". This token cannot express
    either: it is a registered bearer with a baked subject, so there is nothing
    to recompute per request. Refusing is therefore the only honest answer, and
    it is the same call ``_ticket_owner_for_git`` makes for the same reason —
    a write with "no notion of a partial identity" fails closed rather than
    silently widening to the owner. Reachable because ANY ``mcp``-scoped ticket
    satisfies this route and the native chat runner mints one for every chat
    sandbox (``app/chat/manager.py``), so without this a co-session guest or a
    scoped agent could borrow its owner's whole tool surface. Found by Devin
    Review on this PR.
    """
    import time as _time
    import uuid as _uuid
    from datetime import timedelta

    from app.auth.jwt import create_access_token
    from app.auth.public_url import mcp_issuer_url
    from src.repositories import oauth_clients_repo, users_repo

    now = int(_time.time())

    # Authorization first, cache second. The cache exists to avoid re-signing a
    # JWT, never to skip a check: reading it before the guards below made all
    # three of them unreachable on a hit, so a conversation that was deleted —
    # or that became a co-session, or acquired a scope-limited agent — kept
    # serving tools for the rest of the cache's life. Found by Devin Review on
    # this PR, after those guards had already been added and were bypassed by
    # the very fast path above them. The cost is one session read per call,
    # which this route was already paying elsewhere.
    session = chat_session_repo().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="ticket_session_not_found")
    if getattr(session, "is_co_session", False):
        raise HTTPException(status_code=403, detail="mcp_not_available_to_co_session")
    agent_id = getattr(session, "agent_id", None)
    if agent_id:
        from src.agent_scope_intersection import agent_is_passthrough
        from src.repositories import agents_repo

        agent = agents_repo().get_by_id(agent_id)
        if agent is None or agent.get("deleted_at") is not None:
            # Fail CLOSED, exactly as `_mint_identity_jwt` does: a session
            # attributed to a deleted agent must not regain the owner's full
            # authority through the fall-through below.
            raise HTTPException(status_code=401, detail="ticket_agent_not_found")
        if not agent_is_passthrough(agent):
            raise HTTPException(status_code=403, detail="mcp_not_available_to_scoped_agent")
    user = users_repo().get_by_email(session.user_email)
    if user is None:
        raise HTTPException(status_code=401, detail="ticket_user_not_found")

    with _mcp_token_cache_lock:
        cached = _mcp_token_cache.get(session_id)
        if cached and cached[1] - _MCP_ACCESS_TOKEN_REFRESH_MARGIN_SECONDS > now:
            return cached[0]
        _prune_mcp_token_cache(now)

    expires_at = now + _MCP_ACCESS_TOKEN_TTL_SECONDS
    token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        expires_delta=timedelta(seconds=_MCP_ACCESS_TOKEN_TTL_SECONDS),
        token_id=_uuid.uuid4().hex,
        typ="session",
        extra_claims={"scope": "mcp-oauth", "chat_session_id": session_id},
    )
    oauth_clients_repo().save_access_token(
        token=token,
        client_id=_MCP_CLIENT_ID,
        scopes=list(_MCP_SCOPES),
        expires_at=expires_at,
        subject=user["id"],
        # Parity with the genuine code exchange, which passes the client's
        # requested `resource` (`app/auth/mcp_oauth.py`). `load_access_token`
        # copies the stored value through without validating, so a `None` is
        # accepted today — but this IS the resource server the token is for,
        # and pinning it now means an SDK that starts enforcing RFC 8707
        # audience binding does not 401 brokered engine traffic while real
        # connectors keep working. Found by Devin Review on this PR.
        resource=mcp_issuer_url(),
    )
    with _mcp_token_cache_lock:
        _mcp_token_cache[session_id] = (token, expires_at)
    return token


def _require_mcp_surface() -> None:
    """503 unless this instance both embeds the engine and brokers its tools.

    A dependency rather than a line in the handler body: FastAPI resolves a
    signature's dependencies in declaration order and `require_broker_ticket`
    is one of them, so a check written in the body ran *after* the ticket had
    already been evaluated — an unconfigured instance answered `401
    missing_broker_ticket` instead of the 503 that means "this integration is
    not here". Declared first, it decides before any credential is inspected.
    Found by Copilot on this PR.
    """
    _secret()
    if not _broker_mcp_enabled():
        raise HTTPException(status_code=503, detail="kai_mcp_not_enabled")


@router.post("/mcp")
async def kai_mcp(
    request: Request,
    _gate: None = Depends(_require_mcp_surface),
    row: Dict[str, Any] = Depends(require_broker_ticket),
) -> Response:
    """Forward the engine sandbox's MCP request to Agnes's own MCP server,
    under the ticket's real identity.

    Scope-gated on ``mcp``: an ``llm``-scoped ticket cannot reach the tool
    surface, mirroring `_require_scope` on every other broker route.

    The response streams chunk by chunk. A Streamable-HTTP server answers
    either as JSON or as an SSE stream, and a tool that takes a while to
    produce its result must not be buffered whole — the same mistake the
    Anthropic proxy had to fix for token deltas, and here it additionally
    collides with the engine relay's time-to-headers bound (see
    `_mcp_internal_base` for why this is a real HTTP hop and not an in-process
    ASGI dispatch).
    """
    # Same kill switch as every other route — this one authenticates through
    # the broker ticket dependency, which does not pass through
    # `_require_session_credential`, so it asserts for itself.
    # Availability (kill switch + tool switch) is decided by
    # `_require_mcp_surface`, declared ahead of the ticket dependency.
    _require_scope(row, "kai_mcp")
    token = await asyncio.to_thread(_mint_mcp_access_token, row["session_id"])

    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _MCP_DROP_REQUEST_HEADERS}
    headers["Authorization"] = f"Bearer {token}"
    # Dropping `content-encoding` from the RESPONSE (`_MCP_DROP_RESPONSE_HEADERS`)
    # is only sound if what we forward is decoded. Two halves, because either
    # alone is a trap: the sandbox's own `accept-encoding` is dropped, but
    # `build_request` re-adds httpx's default `gzip, deflate`, so the upstream
    # may compress even though nothing downstream asked it to — ask for
    # `identity` so a chunk arrives ready to forward and no decoder sits
    # between a slow tool and the agent. And should an upstream compress
    # anyway, `aiter_bytes()` below decodes rather than `aiter_raw()`, which
    # would emit gzip bytes labelled as plain text.
    headers["Accept-Encoding"] = "identity"

    client = httpx.AsyncClient(base_url=_mcp_internal_base(), timeout=_MCP_PROXY_TIMEOUT)
    try:
        upstream_req = client.build_request("POST", _MCP_STREAMABLE_PATH, headers=headers, content=body)
        upstream = await client.send(upstream_req, stream=True)
    except Exception:
        await client.aclose()
        raise

    passthrough_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _MCP_DROP_RESPONSE_HEADERS}

    async def _body_iter():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        _body_iter(),
        status_code=upstream.status_code,
        headers=passthrough_headers,
        media_type=upstream.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# Workspace payload
#
# The engine materializes an opaque gzipped tarball into its sandbox's project
# scope, where the Claude Agent SDK's `project` setting source discovers it —
# so a shipped tree behaves like a local checkout. We ship the same bundled
# workspace template Agnes's own chat sandbox is prepared from, which is what
# makes the embedded engine feel like the native harness rather than a generic
# agent: the platform CLAUDE.md, the org safety hook, and the bundled skills.
# ---------------------------------------------------------------------------

#: Subtrees of the template that describe how to BUILD an Agnes sandbox rather
#: than how to work inside one. They are meaningless in another engine's
#: sandbox — it has its own image — so they are not shipped. Everything else in
#: the template is workspace content and goes as-is.
_WORKSPACE_EXCLUDED_TOPLEVEL = frozenset({"e2b-template", "docker-sandbox"})

#: Hard ceiling mirroring the engine's own (100 MiB, wire and extracted). The
#: bundled template is ~160 KiB, so this only fires if an operator's override
#: template is enormous — better a clear error here than a failed turn there.
_MAX_WORKSPACE_ARCHIVE_BYTES = 100 * 1024 * 1024


def _workspace_template_root() -> "Path":
    """The tree to pack: an admin-registered Initial Workspace Template when
    one is synced, else the bundled default.

    Same precedence the analyst-facing template flow uses, so the embedded
    engine's sandbox and an analyst's local workspace are prepared from one
    source of truth rather than drifting apart.
    """
    from app.chat.skills_catalog import BUNDLED_TEMPLATE_DIR
    from src.initial_workspace import _WORKSPACE_SUBDIR, _iwt_snapshot

    try:
        # `_iwt_snapshot()` and not a bare `get_initial_workspace_dir().is_dir()`:
        # the YAML is the source of truth, and an admin can unset the template
        # URL while the clone lingers on disk. Probing the filesystem alone
        # would keep shipping a de-registered template to the engine's sandbox
        # — the exact "unset must beat a stale clone" rule `is_configured()`
        # exists to enforce. The snapshot also collapses the two probes into
        # one, so an unset landing mid-call cannot yield a contradictory answer.
        iwt_root = _iwt_snapshot()
        if iwt_root is not None:
            override = iwt_root / _WORKSPACE_SUBDIR
            if override.is_dir():
                return override
    except Exception:
        # A broken/unsynced override must not deny the caller a workspace —
        # fall back to the bundled tree, which is always present.
        logger.warning("kai workspace: override template unreadable, using bundled", exc_info=True)
    return BUNDLED_TEMPLATE_DIR


def _build_workspace_archive() -> Optional[bytes]:
    """Pack the template into the gzipped tar the engine's contract expects,
    or ``None`` when there is nothing to ship.

    Members are relative POSIX paths of regular files only — the engine
    rejects the whole payload on an absolute path, a `..` segment, or any
    non-file member (symlink, device), so those are filtered here rather than
    failing someone's turn. Directories are implicit.
    """
    root = _workspace_template_root()
    if not root.is_dir():
        return None

    buffer = io.BytesIO()
    members = 0
    # Byte-stability needs BOTH timestamps pinned, not just the members': the
    # engine re-fetches on every SDK respawn, and a payload that differs only
    # by timestamp would churn the sandbox tree for no reason.
    #
    # `tarfile.open(mode="w:gz")` builds its own GzipFile with no mtime, so the
    # gzip *container* header carries `time.time()` even when every member
    # carries `mtime=0` — two packs of an identical tree a second apart then
    # differ in 4 header bytes. So the gzip layer is constructed explicitly
    # with `mtime=0` and the tar written into it uncompressed (`w|`).
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w|") as tar:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                rel = path.relative_to(root)
                if rel.parts[0] in _WORKSPACE_EXCLUDED_TOPLEVEL or ".git" in rel.parts:
                    continue
                info = tar.gettarinfo(str(path), arcname=rel.as_posix())
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
                members += 1

    if members == 0:
        return None
    archive = buffer.getvalue()
    if len(archive) > _MAX_WORKSPACE_ARCHIVE_BYTES:
        raise HTTPException(status_code=500, detail="kai_workspace_too_large")
    return archive


@router.get(
    "/workspace",
    # Without this FastAPI also advertises `application/json` on the 200, from
    # its default response class — so the spec offered a media type this route
    # never returns.
    response_class=Response,
    # The generated schema described this 200 as `application/json` — the return
    # annotation is all FastAPI has to go on, and the body is a tarball. Stated
    # explicitly so a client generated from the spec expects bytes. Found by
    # Copilot on this PR.
    responses={
        200: {
            "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}},
            "description": "gzipped tar archive of the caller's workspace tree",
        },
        204: {"description": "this deployment ships no workspace payload"},
    },
)
async def kai_workspace(row: Dict[str, Any] = Depends(_require_session_credential)) -> Response:
    """Serve this caller's workspace tree as one gzipped tarball.

    Closed contract: exactly ``200`` with the archive, or ``204`` for "this
    caller has no workspace". Any other status fails the engine's turn, so
    there is no partial answer — an unreadable template falls back to the
    bundled tree rather than erroring.

    Authenticated by the session credential, not a broker ticket: the engine's
    *server* fetches this once per SDK process spawn, and the sandbox never
    sees it.
    """
    archive = await asyncio.to_thread(_build_workspace_archive)
    if archive is None:
        return Response(status_code=204)
    return Response(content=archive, media_type="application/gzip")
