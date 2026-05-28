"""Tests for the user-facing /api/mcp/passthrough/{tools,tools/{id}/call}.

Cover:

* list: admin sees every enabled passthrough tool; analyst sees only tools
  whose ``tool_grants`` row matches one of their groups.
* invoke: admin can call any; analyst gets 403 without grant, 404 on
  unknown tool, 502 on upstream-call failure.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from src.db import SYSTEM_ADMIN_GROUP, get_system_db
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import (
    MATERIALIZE,
    PASSTHROUGH,
    ToolRegistryRepository,
)
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.user_groups import UserGroupsRepository


def _seed_two_tools_two_groups(analyst_id: str = "analyst1") -> dict:
    """Seed two passthrough tools (granted/not-granted to a new group),
    one materialize tool (must NOT appear in passthrough listing), and
    put ``analyst_id`` in the granted group.

    Returns ids for assertion.
    """
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(
        id="src_test",
        name="test-upstream",
        transport="stdio",
        command="/bin/true",
        args=[],
    )

    # Granted passthrough.
    tools.upsert(
        tool_id="test-upstream.lookup",
        source_id="src_test",
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        description="Granted to analyst's group.",
    )
    # Not granted.
    tools.upsert(
        tool_id="test-upstream.private",
        source_id="src_test",
        original_name="private",
        exposed_name="private",
        mode=PASSTHROUGH,
        description="Not granted.",
    )
    # Materialize — must NOT surface in passthrough listing.
    tools.upsert(
        tool_id="test-upstream.bulk_list",
        source_id="src_test",
        original_name="bulk_list",
        exposed_name="bulk_list",
        mode=MATERIALIZE,
        schedule="every 6h",
    )

    grp = groups.create(name="passthrough-test-grp", description="test grant target")
    granted_gid = grp["id"]
    tools.add_grant("test-upstream.lookup", granted_gid)

    members.add_member(analyst_id, granted_gid, source="system_seed")

    conn.close()
    return {"granted_gid": granted_gid}


# ── /tools (list) ──────────────────────────────────────────────────────────


def test_list_admin_sees_every_enabled_passthrough(seeded_app):
    _seed_two_tools_two_groups()
    client = seeded_app["client"]
    r = client.get(
        "/api/mcp/passthrough/tools",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    ids = {t["tool_id"] for t in payload}
    assert "test-upstream.lookup" in ids
    assert "test-upstream.private" in ids
    # Materialize tools never show up in passthrough listing.
    assert "test-upstream.bulk_list" not in ids


def test_list_analyst_sees_only_granted(seeded_app):
    _seed_two_tools_two_groups()
    client = seeded_app["client"]
    r = client.get(
        "/api/mcp/passthrough/tools",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200, r.text
    ids = {t["tool_id"] for t in r.json()}
    assert ids == {"test-upstream.lookup"}, f"unexpected tools: {ids}"


# ── /tools/{tool_id}/call (invoke) ─────────────────────────────────────────


def _patch_upstream_call(text="ok", is_error=False, data=None, raise_exc=None):
    """Context manager that patches connectors.mcp.client.call_tool_async."""
    from connectors.mcp.client import ToolCallResult

    if raise_exc is not None:
        side_effect = raise_exc
        return_value = None
    else:
        side_effect = None
        return_value = ToolCallResult(text=text, data=data, is_error=is_error)

    return patch(
        "app.api.mcp_passthrough.call_tool_async",
        new=AsyncMock(return_value=return_value, side_effect=side_effect),
    )


def test_invoke_admin_forwards_to_upstream(seeded_app):
    _seed_two_tools_two_groups()
    client = seeded_app["client"]
    with _patch_upstream_call(text='{"hit": 1}', data={"hit": 1}):
        r = client.post(
            "/api/mcp/passthrough/tools/test-upstream.lookup/call",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
            json={"arguments": {"q": "Alice"}},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_error"] is False
    assert body["text"] == '{"hit": 1}'
    assert body["data"] == {"hit": 1}


def test_invoke_analyst_403_without_grant(seeded_app):
    _seed_two_tools_two_groups()
    client = seeded_app["client"]
    r = client.post(
        "/api/mcp/passthrough/tools/test-upstream.private/call",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        json={"arguments": {}},
    )
    assert r.status_code == 403
    assert "no grant" in r.json()["detail"]


def test_invoke_404_for_unknown_tool(seeded_app):
    client = seeded_app["client"]
    r = client.post(
        "/api/mcp/passthrough/tools/test-upstream.does-not-exist/call",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"arguments": {}},
    )
    assert r.status_code == 404


def test_invoke_502_when_upstream_call_blows_up(seeded_app):
    _seed_two_tools_two_groups()
    client = seeded_app["client"]
    with _patch_upstream_call(raise_exc=RuntimeError("upstream gone")):
        r = client.post(
            "/api/mcp/passthrough/tools/test-upstream.lookup/call",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
            json={"arguments": {}},
        )
    assert r.status_code == 502
    assert "upstream gone" in r.json()["detail"]
