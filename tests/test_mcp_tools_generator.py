"""Server-side passthrough callable synthesis (app/api/mcp/tools_generator.py).

Regression guard: a tool with an EMPTY input schema must register a
parameterless callable, NOT a ``**kwargs`` wrapper. FastMCP renders
``**kwargs`` as a required ``kwargs`` field, so empty (the only valid) calls
to a no-arg tool would 422.
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.api.mcp.tools_generator import _make_passthrough_callable
from mcp.server.fastmcp import FastMCP


def _register(input_schema):
    source = {"id": "src1", "name": "fake", "transport": "stdio", "command": "/bin/true"}
    fn = _make_passthrough_callable(source, "noarg_tool", input_schema)
    mcp = FastMCP("Test", instructions="t")
    mcp.add_tool(fn, name="noarg_tool")
    tools = mcp._tool_manager.list_tools()
    return next(t for t in tools if t.name == "noarg_tool")


def test_empty_schema_registers_no_kwargs_param():
    tool = _register({"type": "object", "properties": {}})
    props = (tool.parameters or {}).get("properties") or {}
    required = (tool.parameters or {}).get("required") or []
    assert "kwargs" not in props, f"unexpected kwargs param: {tool.parameters}"
    assert "kwargs" not in required


def test_none_schema_registers_no_kwargs_param():
    tool = _register(None)
    props = (tool.parameters or {}).get("properties") or {}
    assert "kwargs" not in props


def test_unsafe_prop_names_still_use_kwargs():
    tool = _register({"type": "object", "properties": {"weird-key": {"type": "string"}}})
    props = (tool.parameters or {}).get("properties") or {}
    assert "kwargs" in props


def _capture_call_tool_async(monkeypatch):
    """Patch call_tool_async, returning a dict that captures the kwargs of the
    last call and yields a benign ToolCallResult."""
    import app.api.mcp.tools_generator as tg
    from connectors.mcp.client import ToolCallResult

    captured: dict = {}

    async def _fake(source, original_name, *, arguments=None, caller_user_id=None):
        captured["caller_user_id"] = caller_user_id
        captured["arguments"] = arguments
        return ToolCallResult(text="ok", data=None, is_error=False)

    monkeypatch.setattr(tg, "call_tool_async", _fake)
    return captured


def test_passthrough_threads_caller_id_schema_path(monkeypatch):
    """A synthesized (schema'd) closure forwards the caller id from caller_id_fn
    into call_tool_async — so a per_user source resolves the caller's own token."""
    import asyncio

    captured = _capture_call_tool_async(monkeypatch)
    source = {"id": "src1", "name": "fake", "transport": "stdio", "command": "/bin/true"}
    fn = _make_passthrough_callable(
        source, "lookup", {"type": "object", "properties": {"q": {"type": "string"}}},
        caller_id_fn=lambda: "analyst1",
    )
    asyncio.run(fn(q="Alice"))
    assert captured["caller_user_id"] == "analyst1"
    assert captured["arguments"] == {"q": "Alice"}


def test_passthrough_threads_caller_id_kwargs_path(monkeypatch):
    """The **kwargs fallback closure (unsafe prop names) also forwards caller id."""
    import asyncio

    captured = _capture_call_tool_async(monkeypatch)
    source = {"id": "src1", "name": "fake", "transport": "stdio", "command": "/bin/true"}
    fn = _make_passthrough_callable(
        source, "lookup", {"type": "object", "properties": {"weird-key": {"type": "string"}}},
        caller_id_fn=lambda: "analyst1",
    )
    asyncio.run(fn(**{"weird-key": "v"}))
    assert captured["caller_user_id"] == "analyst1"


def test_passthrough_no_caller_id_fn_forwards_none(monkeypatch):
    """Without a caller_id_fn (e.g. no per-request caller), caller_user_id is
    None — the caller-less materialize signal, unchanged behavior."""
    import asyncio

    captured = _capture_call_tool_async(monkeypatch)
    source = {"id": "src1", "name": "fake", "transport": "stdio", "command": "/bin/true"}
    fn = _make_passthrough_callable(source, "lookup", None)
    asyncio.run(fn())
    assert captured["caller_user_id"] is None
