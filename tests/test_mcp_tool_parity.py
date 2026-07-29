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


def test_data_apps_tools_are_foundation_tools():
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    for name in ("data_apps_list", "data_app_get", "data_app_deploy", "data_app_logs"):
        assert name in FOUNDATION_TOOL_NAMES


def test_data_apps_draft_tools_are_foundation_tools():
    """Wave 3B draft/credential MCP tools (Task 8)."""
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    for name in ("data_app_create_draft", "data_app_delete_draft", "data_app_git_credential"):
        assert name in FOUNDATION_TOOL_NAMES


def test_data_apps_preview_tools_are_foundation_tools():
    """Wave 3C in-chat preview loop MCP tools (Task 4) — chat-surface-only,
    no REST/CLI analogue."""
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    for name in (
        "agnes_data_app_preview",
        "agnes_data_app_refresh",
        "agnes_data_app_close",
        "agnes_data_app_credentials",
    ):
        assert name in FOUNDATION_TOOL_NAMES


def test_data_app_tool_names_is_subset_of_foundation():
    """The data-app family constant may not drift from the foundation list."""
    from app.api.mcp.foundation_tools import (
        DATA_APP_TOOL_NAMES,
        FOUNDATION_TOOL_NAMES,
    )

    assert set(DATA_APP_TOOL_NAMES) <= set(FOUNDATION_TOOL_NAMES)


def test_stdio_server_exposes_data_app_family():
    """The CLI stdio ``agnes mcp`` server (cli/mcp/server.py) — the surface the
    in-chat authoring agent connects through — must expose the whole data-app
    family, or the chat agent can neither author/deploy nor emit the
    ``data_app_preview`` render frame (the wave-3C in-chat preview pane).

    The two HTTP foundation transports are covered above; the stdio server is a
    separately hand-maintained curated subset, so it needs its own guard.
    """
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api.mcp.foundation_tools import DATA_APP_TOOL_NAMES
    from cli.mcp import server as stdio_server

    assert set(DATA_APP_TOOL_NAMES) <= _tool_names(stdio_server.mcp)
