"""MCP client wrapper for the inbound Universal MCP connector.

Wraps the official ``mcp`` Python SDK with a small uniform interface used by
``extractor.py`` (materialize) and ``app/api/mcp/passthrough.py`` (live).
Per-call connect/disconnect for POC simplicity — a connection pool can be
layered later for high-frequency passthrough.

Supports three transports:

* ``stdio``  — subprocess launched with ``command`` + ``args``
* ``http``   — Streamable HTTP transport (MCP 2025-03-26+, recommended)
* ``sse``    — legacy SSE transport (HTTP+SSE, MCP 2024-11-05)

Auth is opt-in via ``auth_method`` (``bearer`` / ``basic`` / ``none``) +
``auth_secret_env`` (name of env var holding the token). When the env var
is absent at call time we fall through to anonymous — the POC pattern
matches how ``connectors/keboola`` + ``connectors/bigquery`` already gate
secrets through env, ahead of the §4 vault landing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


def exc_summary(exc: BaseException) -> str:
    """Flatten an exception — including (Base)ExceptionGroup trees — to its
    leaf causes.

    This client's HTTP transports raise through an anyio TaskGroup, so the
    real failure (an httpx 401, or an ``McpError`` carrying the upstream's
    actionable message) arrives wrapped in an ExceptionGroup whose ``str()``
    is just "unhandled errors in a TaskGroup (1 sub-exception)" — useless to
    surface. Callers that stringify our exceptions for humans or agents
    (admin connect probes, the passthrough 502 detail) should route through
    this instead. First line only, deduplicated.
    """
    subs = getattr(exc, "exceptions", None)
    if subs:
        leaves: List[str] = []
        for sub in subs:
            s = exc_summary(sub)
            if s not in leaves:
                leaves.append(s)
        return "; ".join(leaves)
    msg = str(exc).strip()
    first_line = msg.splitlines()[0] if msg else ""
    return f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__


@dataclass
class ToolInfo:
    name: str
    description: Optional[str]
    input_schema: Optional[Dict[str, Any]]


@dataclass
class ToolCallResult:
    """Normalized tool call result.

    ``text`` is the concatenated text of all returned ``TextContent`` blocks
    (the common case for our connectors). ``data`` is ``text`` parsed as JSON
    when the upstream returns a JSON document, else None.
    """

    text: str
    data: Optional[Any]
    is_error: bool


def _to_call_result(content_blocks: List[Any], *, is_error: bool = False) -> ToolCallResult:
    """Reduce MCP content blocks to text + parsed JSON (best-effort)."""
    text_parts: List[str] = []
    for block in content_blocks:
        t = getattr(block, "text", None)
        if t is not None:
            text_parts.append(t)
    text = "\n".join(text_parts)
    data: Optional[Any] = None
    if text:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            data = None
    return ToolCallResult(text=text, data=data, is_error=is_error)


def _lookup_secret_for_source(
    source: Dict[str, Any],
    *,
    caller_user_id: Optional[str] = None,
) -> Optional[str]:
    """Return the upstream auth token for ``source``.

    Precedence depends on ``source['scope']``:

    * ``scope='per_user'`` with a truthy ``caller_user_id`` (an identified,
      interactive caller): only the ``mcp_user_secrets`` row keyed on
      ``(source['id'], caller_user_id)`` is consulted (RFC #461 §4 per-user
      credential passthrough). If there is no such row, this returns
      ``None`` — it does NOT fall back to the shared vault or the env var.
      Fail closed: an identified caller must never borrow the shared
      credential on a per_user source.
    * ``scope='shared'``, or ``scope='per_user'`` with no ``caller_user_id``
      (the caller-less scheduled materialize path): ``mcp_secrets`` row
      keyed on ``source['id']`` (shared vault), else the env var named by
      ``source['auth_secret_env']`` (legacy POC path).

    Returns ``None`` if none yields a value — callers fall through to
    anonymous connect, matching ``auth_method='none'`` behavior.

    ``caller_user_id`` MUST stay ``None`` for scheduled materialize jobs —
    they have no calling user; per-user scope falls back to shared only in
    that caller-less path.
    """
    source_id = source.get("id")
    scope = (source.get("scope") or "shared").lower()

    if source_id:
        try:
            # Local import avoids dragging the vault module into the
            # connector's import surface — keeps stdio MCP startup fast
            # when no DB is around (tests, headless POC scripts).
            #
            # Route through the repo factory: per-user secrets live in the
            # active state backend (Postgres once migrated, #530). Reading them
            # off a raw always-DuckDB connection meant an analyst's own
            # credential was invisible at forward time on a PG instance, so the
            # call silently fell through to the shared/env path.
            from src.repositories import per_user_secrets_repo, shared_secrets_repo

            if scope == "per_user" and caller_user_id:
                value = per_user_secrets_repo().get(source_id, caller_user_id)
                if value:
                    return value
                # Fail closed: an identified caller on a per_user source must
                # NOT borrow the shared credential (or the env-var one). Only
                # the caller-less materialize path (caller_user_id is None)
                # reaches the shared fallback below.
                return None
            # scope='shared', or per_user materialize (caller_user_id is None)
            value = shared_secrets_repo().get(source_id)
            if value:
                return value
        except Exception:
            # System DB unavailable (test fixtures, fresh setup before
            # migration) — silently fall through to the env-var path.
            pass

    secret_env = source.get("auth_secret_env")
    if secret_env and secret_env in os.environ:
        return os.environ[secret_env]
    return None


def _build_http_headers(
    source: Dict[str, Any],
    *,
    caller_user_id: Optional[str] = None,
) -> Dict[str, str]:
    """Build the Authorization header dict for an HTTP/SSE MCP source.

    Returns an empty dict for ``auth_method`` in {``None``, ``""``, ``none``}
    or when no secret is available from vault or env — the caller still
    attempts to connect anonymously, which matches the MCP spec for
    unauthenticated servers and is what the mock fixture does for local
    testing.

    When the source's ``scope`` is ``per_user`` and ``caller_user_id`` is
    provided, the lookup prefers the caller's ``mcp_user_secrets`` row
    before falling back to the shared vault — see
    ``_lookup_secret_for_source`` for the full precedence chain.
    """
    headers: Dict[str, str] = {}
    auth_method = (source.get("auth_method") or "").lower()
    if auth_method in ("", "none"):
        return headers
    token = _lookup_secret_for_source(source, caller_user_id=caller_user_id)
    if not token:
        return headers
    if auth_method == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif auth_method == "basic":
        # token is expected to be "user:pass" — encode it here so operators
        # store the cleartext credential rather than its base64 form (less
        # surprising rotation).
        encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    return headers


# ---------------------------------------------------------------------------
# OAuth token resolution + refresh (2026-07-30 outbound MCP OAuth spec §4)
#
# ``auth_method='oauth'`` sources are ALWAYS ``scope='per_user'`` (enforced
# at ``MCPSourceRepository.upsert()``), so the token lookup below has no
# shared-vault fallback at all — an identified caller with no stored row
# gets no token, and the caller-less materialize path (``caller_user_id`` is
# ``None``) gets no token either (spec: "the caller-less scheduled path gets
# no token", stricter than the secret-vault per_user path which still lets
# materialize borrow the shared vault).
# ---------------------------------------------------------------------------

#: Refresh once the stored access token is within this many seconds of
#: ``expires_at`` (or already expired).
_OAUTH_REFRESH_SKEW_SECONDS = 60
#: Coordination-lease TTL for the cross-process refresh single-flight.
#: Short — a refresh call is one HTTP round-trip, never a long operation.
_OAUTH_REFRESH_LEASE_TTL_S = 30

#: In-process single-flight: one ``asyncio.Lock`` per ``(source_id,
#: user_id)`` pair, created lazily. Guards concurrent coroutines in THIS
#: process from each independently refreshing the same pair; the
#: coordination-backend lease (below) extends that guarantee across
#: processes (Postgres role-split deployments).
_OAUTH_REFRESH_LOCKS: Dict[Tuple[str, str], asyncio.Lock] = {}
_OAUTH_REFRESH_LOCKS_GUARD = threading.Lock()


def _get_oauth_refresh_lock(source_id: str, user_id: str) -> asyncio.Lock:
    key = (source_id, user_id)
    with _OAUTH_REFRESH_LOCKS_GUARD:
        lock = _OAUTH_REFRESH_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _OAUTH_REFRESH_LOCKS[key] = lock
        return lock


def reset_oauth_refresh_locks_for_tests() -> None:
    """Test-only: drop every cached in-process lock between tests."""
    with _OAUTH_REFRESH_LOCKS_GUARD:
        _OAUTH_REFRESH_LOCKS.clear()


def _needs_refresh(row: Dict[str, Any], *, skew_seconds: int = _OAUTH_REFRESH_SKEW_SECONDS) -> bool:
    """True iff ``row['expires_at']`` is within ``skew_seconds`` of now (or
    already past). ``expires_at is None`` means "non-expiring / unknown" —
    never refresh proactively for those."""
    expires_at = row.get("expires_at")
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return (expires_at - datetime.now(timezone.utc)).total_seconds() <= skew_seconds


async def _refresh_oauth_token_with_lease(
    source_id: str,
    user_id: str,
    *,
    holder_id: str,
) -> Optional[str]:
    """Coordination-lease-gated refresh — the cross-process half of the
    single-flight (see the module docstring above). Re-reads the row AFTER
    acquiring the lease (another process may have refreshed it while we
    waited), refreshes only if still needed, persists the rotated token set
    atomically, and deletes the row on ``invalid_grant`` (forces
    re-connect). Returns the (possibly just-refreshed) access token, or
    ``None`` when the row is gone (missing, or just deleted for
    ``invalid_grant``).

    Deliberately takes no ``source`` dict / in-process lock — this is the
    seam ``tests/test_mcp_client_oauth.py``'s two-process race test drives
    directly with two distinct ``holder_id``s, bypassing the in-process
    lock entirely (which real separate OS processes never share) to prove
    the LEASE alone is what caps concurrent refreshes at one.
    """
    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination
    from src.repositories import mcp_source_oauth_clients_repo, mcp_user_oauth_tokens_repo

    tokens_repo = mcp_user_oauth_tokens_repo()
    row = tokens_repo.get(source_id, user_id)
    if row is None:
        return None
    if not _needs_refresh(row):
        return row["access_token"]
    refresh_token = row.get("refresh_token")
    if not refresh_token:
        # No refresh token on file — nothing we can do; hand back the
        # (possibly expired) access token and let the upstream reject it.
        return row["access_token"]

    client_row = mcp_source_oauth_clients_repo().get(source_id)
    if client_row is None:
        return row["access_token"]

    lease_name = f"mcp_oauth_refresh:{source_id}:{user_id}"
    try:
        acquired = coordination().lease_acquire(lease_name, holder_id, ttl_s=_OAUTH_REFRESH_LEASE_TTL_S)
    except CoordinationUnavailable:
        # Fail OPEN: a down coordination backend must not wedge every MCP
        # call on an oauth source — proceed unserialized (the in-process
        # lock still protects same-process callers).
        acquired = True

    if not acquired:
        # Another process holds the lease and is refreshing right now.
        # Poll briefly for the winner's write to land instead of instantly
        # handing back the stale token (which would surface as an opaque
        # upstream 401 — Devin Review on #1124); never double-refresh.
        fresh = None
        for _ in range(10):
            await asyncio.sleep(0.2)
            fresh = tokens_repo.get(source_id, user_id)
            if fresh is None or not _needs_refresh(fresh):
                break
        return fresh["access_token"] if fresh else None

    try:
        from connectors.mcp.oauth_client import (
            OAuthTokenError,
            build_oauth_http_client,
            is_invalid_grant_error,
            refresh_access_token,
        )

        try:
            async with build_oauth_http_client() as http_client:
                token_set = await refresh_access_token(
                    token_endpoint=client_row["token_endpoint"],
                    client_id=client_row["client_id"],
                    client_secret=client_row.get("client_secret"),
                    refresh_token=refresh_token,
                    client=http_client,
                )
        except OAuthTokenError as exc:
            if is_invalid_grant_error(exc):
                tokens_repo.delete(source_id, user_id)
                logger.warning(
                    "mcp oauth refresh invalid_grant for source=%s user=%s; row deleted, re-connect required",
                    source_id,
                    user_id,
                )
                return None
            logger.warning(
                "mcp oauth refresh failed for source=%s user=%s: %s",
                source_id,
                user_id,
                exc_summary(exc),
            )
            return row["access_token"]

        expires_at = None
        if token_set.expires_in is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_set.expires_in)
        # Rotated refresh tokens are persisted atomically with the new
        # access token — a single upsert, one row write.
        tokens_repo.upsert(
            source_id,
            user_id,
            token_set.access_token,
            refresh_token=token_set.refresh_token or refresh_token,
            expires_at=expires_at,
            scopes=token_set.scopes or row.get("scopes"),
        )
        return token_set.access_token
    finally:
        try:
            coordination().lease_release(lease_name, holder_id)
        except CoordinationUnavailable:
            pass


async def _resolve_oauth_access_token(
    source: Dict[str, Any],
    caller_user_id: Optional[str],
) -> Optional[str]:
    """Resolve (and refresh if needed) the caller's OAuth access token for
    an ``auth_method='oauth'`` source. Fail-closed, no shared fallback."""
    source_id = source.get("id")
    if not source_id or not caller_user_id:
        return None
    from src.repositories import mcp_user_oauth_tokens_repo

    row = mcp_user_oauth_tokens_repo().get(source_id, caller_user_id)
    if row is None:
        return None
    if not _needs_refresh(row):
        return row["access_token"]

    from app.coordination.leases import default_holder_id

    lock = _get_oauth_refresh_lock(source_id, caller_user_id)
    async with lock:
        return await _refresh_oauth_token_with_lease(
            source_id,
            caller_user_id,
            holder_id=default_holder_id(),
        )


async def _resolve_http_headers_async(
    source: Dict[str, Any],
    *,
    caller_user_id: Optional[str] = None,
) -> Dict[str, str]:
    """Async superset of :func:`_build_http_headers` — adds the
    ``auth_method='oauth'`` branch (token resolution needs I/O to refresh);
    every other auth method delegates unchanged to the sync helper."""
    auth_method = (source.get("auth_method") or "").lower()
    if auth_method == "oauth":
        token = await _resolve_oauth_access_token(source, caller_user_id)
        return {"Authorization": f"Bearer {token}"} if token else {}
    return _build_http_headers(source, caller_user_id=caller_user_id)


@asynccontextmanager
async def _open_session(
    source: Dict[str, Any],
    *,
    caller_user_id: Optional[str] = None,
) -> AsyncIterator[ClientSession]:
    """Open an MCP session for the given source row (see mcp_sources schema).

    Routes to one of three SDK transports based on ``source['transport']``:

    * ``stdio`` — ``mcp.client.stdio.stdio_client`` with the command/args.
    * ``http``  — ``mcp.client.streamable_http.streamablehttp_client``
      (MCP 2025-03-26+; the recommended transport for new servers).
    * ``sse``   — ``mcp.client.sse.sse_client`` (legacy HTTP+SSE).

    ``caller_user_id`` is propagated through to the secret lookup so
    sources with ``scope='per_user'`` resolve the analyst's own token.
    """
    transport = (source.get("transport") or "").lower()

    if transport == "stdio":
        command = source["command"]
        args = source.get("args") or []
        # ``args`` may already be a list (after repo decode) or a JSON string
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = []

        # Per-source non-secret env (e.g. CRM_API_URL) is the base; the
        # auth_secret_env secret overlays it (takes precedence).
        env_extra: Dict[str, str] = dict(source.get("env") or {})
        secret_env = source.get("auth_secret_env")
        if secret_env:
            # Vault first, env-var second — same precedence as the HTTP
            # path so an admin who migrated a source from env-var to
            # vault doesn't have to keep both populated. The vault path
            # writes the decrypted value under the original env-var
            # name the upstream MCP server expects, so the subprocess
            # contract stays unchanged.
            token = _lookup_secret_for_source(source, caller_user_id=caller_user_id)
            if token:
                env_extra[secret_env] = token

        params = StdioServerParameters(command=command, args=list(args), env=env_extra or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    if transport in ("http", "sse"):
        url = source.get("url")
        if not url:
            raise ValueError(f"{transport!r} transport requires 'url'")
        headers = await _resolve_http_headers_async(source, caller_user_id=caller_user_id)

        if transport == "http":
            # streamablehttp_client yields (read, write, get_session_id) — we
            # ignore the session-id callable for now (no resume support yet).
            async with streamablehttp_client(url, headers=headers or None) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:  # sse
            async with sse_client(url, headers=headers or None) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        return

    raise NotImplementedError(f"transport {transport!r} not supported (expected stdio | http | sse)")


async def list_tools_async(
    source: Dict[str, Any],
    *,
    caller_user_id: Optional[str] = None,
) -> List[ToolInfo]:
    """List the upstream's tools.

    ``caller_user_id`` is threaded to the secret lookup so a ``scope='per_user'``
    source is introspected under the caller's own credential (used by the
    per-user ``…/my-secret/test`` endpoint). Materialize/admin callers leave it
    ``None`` and stay on the shared vault path.
    """
    async with _open_session(source, caller_user_id=caller_user_id) as session:
        result = await session.list_tools()
        out: List[ToolInfo] = []
        for t in result.tools:
            schema = getattr(t, "inputSchema", None)
            out.append(ToolInfo(name=t.name, description=t.description, input_schema=schema))
        return out


def list_tools(source: Dict[str, Any], *, caller_user_id: Optional[str] = None) -> List[ToolInfo]:
    """Sync wrapper around list_tools_async."""
    return asyncio.run(list_tools_async(source, caller_user_id=caller_user_id))


async def call_tool_async(
    source: Dict[str, Any],
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
    *,
    caller_user_id: Optional[str] = None,
) -> ToolCallResult:
    """Forward a single ``tool_name`` call to the upstream MCP described
    by ``source``.

    ``caller_user_id`` is threaded to the secret lookup so per-user
    scoped sources see the right credential. Materialize jobs (which
    have no calling user) leave it at ``None`` and stay on the shared
    vault / env-var path.
    """
    async with _open_session(source, caller_user_id=caller_user_id) as session:
        result = await session.call_tool(tool_name, arguments or {})
        is_error = bool(getattr(result, "isError", False))
        return _to_call_result(result.content, is_error=is_error)


def call_tool(
    source: Dict[str, Any],
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
    *,
    caller_user_id: Optional[str] = None,
) -> ToolCallResult:
    """Sync wrapper around call_tool_async."""
    return asyncio.run(
        call_tool_async(
            source,
            tool_name,
            arguments,
            caller_user_id=caller_user_id,
        )
    )
