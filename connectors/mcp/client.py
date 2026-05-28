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
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


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


def _lookup_secret_for_source(source: Dict[str, Any]) -> Optional[str]:
    """Return the upstream auth token for ``source`` from one of two
    places, in order:

    1. ``mcp_secrets`` row keyed on ``source['id']`` (Phase 4 vault).
    2. Env var named by ``source['auth_secret_env']`` (legacy POC path).

    Returns ``None`` if neither yields a value — callers fall through
    to anonymous connect, matching ``auth_method='none'`` behavior.
    The vault path opens a fresh system.duckdb connection on each call
    rather than threading a connection through ``call_tool_async``;
    cost is one cheap file-open per upstream call (negligible vs the
    network roundtrip). Keeps the connector free of FastAPI/DB plumbing.
    """
    source_id = source.get("id")
    if source_id:
        try:
            # Local import avoids dragging the vault module into the
            # connector's import surface — keeps stdio MCP startup fast
            # when no DB is around (tests, headless POC scripts).
            from src.db import get_system_db
            from app.secrets_vault import SharedSecretsRepository

            conn = get_system_db()
            try:
                value = SharedSecretsRepository(conn).get(source_id)
                if value:
                    return value
            finally:
                conn.close()
        except Exception:
            # System DB unavailable (test fixtures, fresh setup before
            # migration) — silently fall through to the env-var path.
            pass

    secret_env = source.get("auth_secret_env")
    if secret_env and secret_env in os.environ:
        return os.environ[secret_env]
    return None


def _build_http_headers(source: Dict[str, Any]) -> Dict[str, str]:
    """Build the Authorization header dict for an HTTP/SSE MCP source.

    Returns an empty dict for ``auth_method`` in {``None``, ``""``, ``none``}
    or when no secret is available from vault or env — the caller still
    attempts to connect anonymously, which matches the MCP spec for
    unauthenticated servers and is what the mock fixture does for local
    testing.
    """
    headers: Dict[str, str] = {}
    auth_method = (source.get("auth_method") or "").lower()
    if auth_method in ("", "none"):
        return headers
    token = _lookup_secret_for_source(source)
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


@asynccontextmanager
async def _open_session(source: Dict[str, Any]) -> AsyncIterator[ClientSession]:
    """Open an MCP session for the given source row (see mcp_sources schema).

    Routes to one of three SDK transports based on ``source['transport']``:

    * ``stdio`` — ``mcp.client.stdio.stdio_client`` with the command/args.
    * ``http``  — ``mcp.client.streamable_http.streamablehttp_client``
      (MCP 2025-03-26+; the recommended transport for new servers).
    * ``sse``   — ``mcp.client.sse.sse_client`` (legacy HTTP+SSE).
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

        env_extra: Optional[Dict[str, str]] = None
        secret_env = source.get("auth_secret_env")
        if secret_env:
            # Vault first, env-var second — same precedence as the HTTP
            # path so an admin who migrated a source from env-var to
            # vault doesn't have to keep both populated. The vault path
            # writes the decrypted value under the original env-var
            # name the upstream MCP server expects, so the subprocess
            # contract stays unchanged.
            token = _lookup_secret_for_source(source)
            if token:
                env_extra = {secret_env: token}

        params = StdioServerParameters(command=command, args=list(args), env=env_extra)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    if transport in ("http", "sse"):
        url = source.get("url")
        if not url:
            raise ValueError(f"{transport!r} transport requires 'url'")
        headers = _build_http_headers(source)

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


async def list_tools_async(source: Dict[str, Any]) -> List[ToolInfo]:
    async with _open_session(source) as session:
        result = await session.list_tools()
        out: List[ToolInfo] = []
        for t in result.tools:
            schema = getattr(t, "inputSchema", None)
            out.append(ToolInfo(name=t.name, description=t.description, input_schema=schema))
        return out


def list_tools(source: Dict[str, Any]) -> List[ToolInfo]:
    """Sync wrapper around list_tools_async."""
    return asyncio.run(list_tools_async(source))


async def call_tool_async(
    source: Dict[str, Any],
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
) -> ToolCallResult:
    async with _open_session(source) as session:
        result = await session.call_tool(tool_name, arguments or {})
        is_error = bool(getattr(result, "isError", False))
        return _to_call_result(result.content, is_error=is_error)


def call_tool(
    source: Dict[str, Any],
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
) -> ToolCallResult:
    """Sync wrapper around call_tool_async."""
    return asyncio.run(call_tool_async(source, tool_name, arguments))
