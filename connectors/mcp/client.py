"""MCP client wrapper for the inbound Universal MCP connector.

Wraps the official ``mcp`` Python SDK with a small uniform interface used by
``extractor.py`` (materialize) and ``app/api/mcp/passthrough.py`` (live).
Per-call connect/disconnect for POC simplicity — a connection pool can be
layered later for high-frequency passthrough.

Supports stdio transport (subprocess) today; HTTP/SSE follows the same shape
and can be wired in by switching the context manager.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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


@asynccontextmanager
async def _open_session(source: Dict[str, Any]) -> AsyncIterator[ClientSession]:
    """Open an MCP session for the given source row (see mcp_sources schema)."""
    transport = source["transport"]
    if transport != "stdio":
        raise NotImplementedError(f"transport {transport!r} not implemented yet (POC: stdio only)")

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
    if secret_env and secret_env in os.environ:
        # Pass the named secret through unchanged so the upstream MCP can read it.
        env_extra = {secret_env: os.environ[secret_env]}

    params = StdioServerParameters(command=command, args=list(args), env=env_extra)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


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
