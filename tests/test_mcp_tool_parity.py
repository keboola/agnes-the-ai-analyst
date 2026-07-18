"""Both MCP transports must expose the same foundation tools.

Root cause of the drift this guards against: mcp_streamable.py hand-duplicated
6 of the 24 foundation tools defined in mcp_http.py, so a remote OAuth
connector (streamable transport) silently lost knowledge_search,
collections_*, skills, chat_skills, stack_*, store_*, and admin tools. Both
transports now register from the shared `app.api.mcp.foundation_tools` module.
"""

from __future__ import annotations

import asyncio

import pytest


def _tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_sse_exposes_all_foundation_tools():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api import mcp_http
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    assert set(FOUNDATION_TOOL_NAMES) <= _tool_names(mcp_http.mcp)


def test_streamable_exposes_all_foundation_tools(seeded_app):
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    app = seeded_app["client"].app
    mcp = app.state.mcp_streamable_instance
    assert mcp is not None, "streamable MCP instance was not mounted (check SERVER_URL/AGNES_BASE_URL in env)"

    assert set(FOUNDATION_TOOL_NAMES) <= _tool_names(mcp)


def test_glossary_search_is_a_foundation_tool():
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    assert "glossary_search" in FOUNDATION_TOOL_NAMES
