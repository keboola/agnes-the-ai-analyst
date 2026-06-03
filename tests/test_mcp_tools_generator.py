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
