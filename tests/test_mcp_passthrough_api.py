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


# ── Policy gates (mutating / rate-limit / PII redact) ──────────────────────


def _seed_mutating_tool(*, analyst_id: str = "analyst1") -> None:
    """Seed a single passthrough tool with ``mutating=True``, granted to
    the analyst's group so RBAC doesn't 403 before the mutating gate."""
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(id="src_mut", name="mut-up", transport="stdio", command="/bin/true", args=[])
    tools.upsert(
        tool_id="mut-up.delete_all",
        source_id="src_mut",
        original_name="delete_all",
        exposed_name="delete_all",
        mode=PASSTHROUGH,
        description="Wipes the upstream.",
        mutating=True,
    )
    grp = groups.create(name="mut-grant-grp", description=None)
    tools.add_grant("mut-up.delete_all", grp["id"])
    members.add_member(analyst_id, grp["id"], source="system_seed")
    conn.close()


def test_invoke_mutating_blocked_for_analyst(seeded_app):
    _seed_mutating_tool()
    client = seeded_app["client"]
    with _patch_upstream_call(text="should-not-reach"):
        r = client.post(
            "/api/mcp/passthrough/tools/mut-up.delete_all/call",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
            json={"arguments": {}},
        )
    assert r.status_code == 403
    assert "mutating" in r.json()["detail"]


def test_invoke_mutating_allowed_for_admin(seeded_app):
    _seed_mutating_tool()
    client = seeded_app["client"]
    with _patch_upstream_call(text="deleted"):
        r = client.post(
            "/api/mcp/passthrough/tools/mut-up.delete_all/call",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
            json={"arguments": {}},
        )
    assert r.status_code == 200
    assert r.json()["text"] == "deleted"


def _seed_pii_tool(*, analyst_id: str = "analyst1") -> None:
    """Seed a passthrough tool with ``pii_fields=['email']`` for redact tests."""
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(id="src_pii", name="pii-up", transport="stdio", command="/bin/true", args=[])
    tools.upsert(
        tool_id="pii-up.lookup",
        source_id="src_pii",
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        pii_fields=["email"],
    )
    grp = groups.create(name="pii-grant-grp", description=None)
    tools.add_grant("pii-up.lookup", grp["id"])
    members.add_member(analyst_id, grp["id"], source="system_seed")
    conn.close()


def test_invoke_redacts_pii_in_response(seeded_app):
    _seed_pii_tool()
    client = seeded_app["client"]
    with _patch_upstream_call(
        text='{"email": "a@x", "name": "Alice"}',
        data={"email": "a@x", "name": "Alice"},
    ):
        r = client.post(
            "/api/mcp/passthrough/tools/pii-up.lookup/call",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
            json={"arguments": {}},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["email"] == "[REDACTED]"
    assert body["data"]["name"] == "Alice"
    assert "a@x" not in body["text"]


def _seed_rate_limited_tool(*, analyst_id: str = "analyst1", cap: int = 2) -> None:
    """Seed a passthrough tool with ``rate_limit_pm=cap`` for rate tests."""
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(id="src_rl", name="rl-up", transport="stdio", command="/bin/true", args=[])
    tools.upsert(
        tool_id="rl-up.search",
        source_id="src_rl",
        original_name="search",
        exposed_name="search",
        mode=PASSTHROUGH,
        rate_limit_pm=cap,
    )
    grp = groups.create(name="rl-grant-grp", description=None)
    tools.add_grant("rl-up.search", grp["id"])
    members.add_member(analyst_id, grp["id"], source="system_seed")
    conn.close()


def test_invoke_rate_limit_returns_429_after_cap(seeded_app):
    from app.api.mcp_policy import reset_rate_buckets_for_tests
    reset_rate_buckets_for_tests()
    _seed_rate_limited_tool(cap=2)
    client = seeded_app["client"]
    with _patch_upstream_call(text="ok"):
        # Two calls within the same window succeed
        for _ in range(2):
            r = client.post(
                "/api/mcp/passthrough/tools/rl-up.search/call",
                headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
                json={"arguments": {}},
            )
            assert r.status_code == 200, r.text
        # Third call within the same minute → 429 + Retry-After header
        r = client.post(
            "/api/mcp/passthrough/tools/rl-up.search/call",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
            json={"arguments": {}},
        )
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    reset_rate_buckets_for_tests()


def _seed_per_user_passthrough_tool(analyst_id: str = "analyst1") -> None:
    """Seed a ``scope='per_user'`` source with one passthrough tool granted to
    a group ``analyst_id`` belongs to — but store NO per-user secret. Used to
    exercise the fail-closed guard.
    """
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    sources.upsert(
        id="src_pu_pt",
        name="pu-upstream",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="bearer",
        scope="per_user",
    )
    tools.upsert(
        tool_id="pu-upstream.lookup",
        source_id="src_pu_pt",
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        description="Per-user source, granted but no personal secret.",
    )
    grp = groups.create(name="pu-passthrough-grp", description="test grant target")
    tools.add_grant("pu-upstream.lookup", grp["id"])
    members.add_member(analyst_id, grp["id"], source="system_seed")
    conn.close()


def test_invoke_per_user_no_secret_returns_403_and_does_not_forward(seeded_app):
    """Granted caller on a per_user source with NO personal credential → 403
    with the my-secret remedy, and the upstream connector is never called."""
    _seed_per_user_passthrough_tool()
    client = seeded_app["client"]
    with _patch_upstream_call(text="LEAK") as mock:
        r = client.post(
            "/api/mcp/passthrough/tools/pu-upstream.lookup/call",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
            json={"arguments": {}},
        )
    assert r.status_code == 403, r.text
    assert "my-secret" in r.json()["detail"]
    mock.assert_not_called()
