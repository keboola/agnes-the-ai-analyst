"""SSE / Streamable-HTTP passthrough closures enforce the same gate stack as REST.

The server-hosted MCP transports (``app/api/mcp_http.py`` SSE and
``app/api/mcp_streamable.py`` Streamable-HTTP) register passthrough tool
closures via ``app/api/mcp/tools_generator.register_passthrough_tools``. Those
closures used to call ``call_tool_async`` directly, bypassing the RBAC + policy
gates the REST endpoint (``invoke_passthrough_tool``) enforces: per-group grant
visibility, the mutating gate, and the per-(tool,user) rate limit.

These tests drive the synthesized closures with different caller identities and
assert the gate now fires — a non-granted caller cannot reach the upstream, a
mutating tool is admin-only, the rate limit trips, PII is redacted, and an
unresolved caller identity fails closed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from connectors.mcp.client import ToolCallResult
from mcp.server.fastmcp import FastMCP
from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.user_groups import UserGroupsRepository


# ── seeding helpers ──────────────────────────────────────────────────────────


def _seed_tool(
    *,
    tool_id: str = "up.lookup",
    original_name: str = "lookup",
    exposed_name: str = "lookup",
    grant_to_analyst: bool = True,
    mutating: bool = False,
    rate_limit_pm=None,
    pii_fields=None,
    analyst_id: str = "analyst1",
) -> None:
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(id="src_up", name="up", transport="stdio", command="/bin/true", args=[])
    tools.upsert(
        tool_id=tool_id,
        source_id="src_up",
        original_name=original_name,
        exposed_name=exposed_name,
        mode=PASSTHROUGH,
        description="test",
        mutating=mutating,
        rate_limit_pm=rate_limit_pm,
        pii_fields=pii_fields,
    )
    grp = groups.create(name=f"grp-{tool_id}", description=None)
    if grant_to_analyst:
        tools.add_grant(tool_id, grp["id"])
        members.add_member(analyst_id, grp["id"], source="system_seed")
    conn.close()


def _closure(exposed_name: str, caller_id_fn):
    """Register passthrough tools on a fresh FastMCP and return the closure fn."""
    from app.api.mcp.tools_generator import register_passthrough_tools

    mcp = FastMCP("Test", instructions="t")
    register_passthrough_tools(mcp, caller_id_fn=caller_id_fn)
    return mcp._tool_manager.get_tool(exposed_name).fn


def _patch_upstream(text="ok", data=None, is_error=False):
    return patch(
        "app.api.mcp.tools_generator.call_tool_async",
        new=AsyncMock(return_value=ToolCallResult(text=text, data=data, is_error=is_error)),
    )


# ── grant gate ───────────────────────────────────────────────────────────────


def test_granted_analyst_reaches_upstream(seeded_app):
    _seed_tool(grant_to_analyst=True)
    fn = _closure("lookup", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="hit") as mock:
        out = asyncio.run(fn())
    assert out == "hit"
    mock.assert_awaited_once()


def test_non_granted_analyst_denied_and_no_forward(seeded_app):
    _seed_tool(tool_id="up.private", exposed_name="private", grant_to_analyst=False)
    fn = _closure("private", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="no grant"):
            asyncio.run(fn())
    mock.assert_not_called()


def test_admin_reaches_upstream_without_grant(seeded_app):
    _seed_tool(tool_id="up.private2", exposed_name="private2", grant_to_analyst=False)
    fn = _closure("private2", caller_id_fn=lambda: "admin1")
    with _patch_upstream(text="ok") as mock:
        out = asyncio.run(fn())
    assert out == "ok"
    mock.assert_awaited_once()


def test_unresolved_caller_fails_closed(seeded_app):
    """caller_id_fn returning None (bad/absent token) → non-admin, no groups →
    grant check fails closed; upstream never reached."""
    _seed_tool(grant_to_analyst=True)
    fn = _closure("lookup", caller_id_fn=lambda: None)
    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="no grant"):
            asyncio.run(fn())
    mock.assert_not_called()


# ── mutating gate ──────────────────────────────────────────────────────────


def test_mutating_blocked_for_non_admin(seeded_app):
    _seed_tool(tool_id="up.del", exposed_name="del", mutating=True, grant_to_analyst=True)
    fn = _closure("del", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="LEAK") as mock:
        with pytest.raises(RuntimeError, match="mutating"):
            asyncio.run(fn())
    mock.assert_not_called()


def test_mutating_allowed_for_admin(seeded_app):
    _seed_tool(tool_id="up.del2", exposed_name="del2", mutating=True, grant_to_analyst=False)
    fn = _closure("del2", caller_id_fn=lambda: "admin1")
    with _patch_upstream(text="deleted") as mock:
        out = asyncio.run(fn())
    assert out == "deleted"
    mock.assert_awaited_once()


# ── rate limit gate ─────────────────────────────────────────────────────────


def test_rate_limit_trips_after_cap(seeded_app):
    from app.api.mcp_policy import reset_rate_buckets_for_tests

    reset_rate_buckets_for_tests()
    _seed_tool(tool_id="up.search", exposed_name="search", rate_limit_pm=2, grant_to_analyst=True)
    fn = _closure("search", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text="ok"):
        asyncio.run(fn())
        asyncio.run(fn())
        with pytest.raises(RuntimeError, match="rate limit"):
            asyncio.run(fn())
    reset_rate_buckets_for_tests()


# ── PII redaction parity ─────────────────────────────────────────────────────


def test_pii_redacted_in_closure_output(seeded_app):
    _seed_tool(tool_id="up.pii", exposed_name="piilookup", pii_fields=["email"], grant_to_analyst=True)
    fn = _closure("piilookup", caller_id_fn=lambda: "analyst1")
    with _patch_upstream(text='{"email": "a@x", "name": "Alice"}', data={"email": "a@x", "name": "Alice"}):
        out = asyncio.run(fn())
    assert "a@x" not in out
    assert "[REDACTED]" in out
    assert "Alice" in out


# ── shared gate unit ─────────────────────────────────────────────────────────


def test_enforce_passthrough_access_shared_by_rest_and_transports(seeded_app):
    """The extracted gate raises the typed exceptions both paths map from."""
    from app.api.mcp_policy import GrantDenied, MutatingNotAllowed, enforce_passthrough_access

    _seed_tool(tool_id="up.g", exposed_name="g", grant_to_analyst=True)
    conn = get_system_db()
    tool = ToolRegistryRepository(conn).get("up.g")
    conn.close()

    # Granted analyst passes.
    enforce_passthrough_access(tool, "analyst1")
    # Unknown caller fails closed.
    with pytest.raises(GrantDenied):
        enforce_passthrough_access(tool, "nobody")
    # Admin short-circuits grant.
    enforce_passthrough_access(tool, "admin1")
    # Mutating tool blocks the granted non-admin.
    tool["mutating"] = True
    with pytest.raises(MutatingNotAllowed):
        enforce_passthrough_access(tool, "analyst1")
