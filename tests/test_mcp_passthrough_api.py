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

from src.db import get_system_db
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


def test_invoke_per_user_no_secret_403_carries_web_deep_link(seeded_app, monkeypatch):
    """With a public URL configured, the 403 remedy that reaches the caller
    (and therefore the agent) is the web-first deep link to the connect page —
    keyed by source id."""
    import app.api.mcp_policy as policy

    monkeypatch.setattr(policy, "get_public_url", lambda: "https://agnes.example", raising=False)
    _seed_per_user_passthrough_tool()
    client = seeded_app["client"]
    with _patch_upstream_call(text="LEAK") as mock:
        r = client.post(
            "/api/mcp/passthrough/tools/pu-upstream.lookup/call",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
            json={"arguments": {}},
        )
    assert r.status_code == 403, r.text
    assert "https://agnes.example/me/connections?source=src_pu_pt" in r.json()["detail"]
    mock.assert_not_called()


def _list_names(caller_id, seed_analyst="analyst1"):
    """Register the seeded passthrough tools on a bare FastMCP, install the
    grant filter with a fixed caller, and return the tool names tools/list
    would advertise to that caller."""
    import asyncio

    from mcp.server.fastmcp import FastMCP

    from app.api.mcp.tools_generator import (
        install_grant_filtered_list_tools,
        register_passthrough_tools,
    )

    mcp = FastMCP("Test", instructions="t")
    register_passthrough_tools(mcp)
    filtered = install_grant_filtered_list_tools(mcp, caller_id_fn=lambda: caller_id)
    tools = asyncio.run(filtered())
    return {t.name for t in tools}


def test_list_tools_hides_ungranted_passthrough_for_analyst(seeded_app):
    """tools/list on the transports shows a non-admin only the passthrough
    tools their groups are granted — matching the REST listing's visibility."""
    _seed_two_tools_two_groups()  # analyst1 granted 'lookup', not 'private'
    names = _list_names("analyst1")
    assert "lookup" in names
    assert "private" not in names


def test_list_tools_admin_sees_all_passthrough(seeded_app):
    _seed_two_tools_two_groups()
    names = _list_names("admin1")
    assert {"lookup", "private"} <= names


def test_list_tools_unresolved_caller_sees_no_passthrough(seeded_app):
    """No resolvable caller identity → fail closed: no passthrough tools listed."""
    _seed_two_tools_two_groups()
    names = _list_names(None)
    assert "lookup" not in names
    assert "private" not in names


def test_list_tools_fails_closed_when_grant_resolution_raises(seeded_app):
    """If resolving the caller's grants blows up at request time (e.g. the
    registry/DB is unreachable), the filter must hide the ENTIRE known
    passthrough universe rather than fall through to the unfiltered list —
    the leak this control exists to prevent."""
    import asyncio
    from unittest.mock import patch

    from mcp.server.fastmcp import FastMCP

    from app.api.mcp.tools_generator import (
        install_grant_filtered_list_tools,
        register_passthrough_tools,
    )

    _seed_two_tools_two_groups()
    mcp = FastMCP("Test", instructions="t")
    registered = register_passthrough_tools(mcp)
    assert {"lookup", "private"} <= set(registered)  # universe is non-empty
    # Even an admin caller: the grant lookup itself raises.
    filtered = install_grant_filtered_list_tools(mcp, caller_id_fn=lambda: "admin1", passthrough_names=registered)
    with patch(
        "app.api.mcp.tools_generator._allowed_passthrough_names",
        side_effect=RuntimeError("DB unavailable"),
    ):
        names = {t.name for t in asyncio.run(filtered())}
    assert "lookup" not in names
    assert "private" not in names


# ── /my-secret/test endpoint ────────────────────────────────────────────────


def _seed_ungranted_per_user_source() -> None:
    """A per_user source with a tool the analyst is NOT granted."""
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    sources.upsert(
        id="src_pu_ung", name="pu-ungranted", transport="http", url="https://up.example/mcp", scope="per_user"
    )
    tools.upsert(
        tool_id="pu-ungranted.lookup",
        source_id="src_pu_ung",
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        description="ungranted",
    )
    conn.close()


def _store_analyst_secret(monkeypatch, source_id: str, value: str) -> None:
    from cryptography.fernet import Fernet
    from src.repositories import per_user_secrets_repo

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode("ascii"))
    per_user_secrets_repo().upsert(source_id, "analyst1", value)


def test_test_endpoint_rejects_shared_scope(seeded_app):
    _seed_two_tools_two_groups()  # src_test is scope=shared, lookup granted
    r = seeded_app["client"].post(
        "/api/mcp/sources/src_test/my-secret/test", headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"}
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "source_scope_not_per_user"


def test_test_endpoint_requires_grant(seeded_app):
    _seed_ungranted_per_user_source()
    r = seeded_app["client"].post(
        "/api/mcp/sources/src_pu_ung/my-secret/test", headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"}
    )
    assert r.status_code == 403, r.text


def test_test_endpoint_no_credential_403_no_upstream(seeded_app):
    _seed_per_user_passthrough_tool()  # granted, but no stored secret
    with patch("connectors.mcp.client.list_tools_async", new=AsyncMock()) as up:
        r = seeded_app["client"].post(
            "/api/mcp/sources/src_pu_pt/my-secret/test",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        )
    assert r.status_code == 403, r.text
    assert "my-secret" in r.json()["detail"]  # CLI remedy (no public url in tests)
    up.assert_not_called()


def test_test_endpoint_ok_threads_caller_id(seeded_app, monkeypatch):
    _seed_per_user_passthrough_tool()
    _store_analyst_secret(monkeypatch, "src_pu_pt", "tok")
    fake = AsyncMock(return_value=[1, 2])
    with patch("connectors.mcp.client.list_tools_async", new=fake):
        r = seeded_app["client"].post(
            "/api/mcp/sources/src_pu_pt/my-secret/test",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "tool_count": 2, "message": "ok"}
    # Plumbing guard: the upstream introspection runs under the caller's id.
    assert fake.await_args.kwargs["caller_user_id"] == "analyst1"


def test_test_endpoint_redacts_token_before_truncation(seeded_app, monkeypatch):
    _seed_per_user_passthrough_tool()
    _store_analyst_secret(monkeypatch, "src_pu_pt", "SEKRET")
    boom = AsyncMock(side_effect=RuntimeError("401 bad token SEKRET " + "x" * 500))
    with patch("connectors.mcp.client.list_tools_async", new=boom):
        r = seeded_app["client"].post(
            "/api/mcp/sources/src_pu_pt/my-secret/test",
            headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
        )
    body = r.json()
    assert body["ok"] is False
    # User sees a friendly, actionable line — not a raw SDK/TaskGroup string —
    # and it never contains the token.
    assert "Couldn't connect" in body["message"]
    assert "SEKRET" not in body["message"]
    assert "TaskGroup" not in body["message"]
    assert len(body["message"]) <= 300


def test_test_endpoint_over_rate_limit_429(seeded_app, monkeypatch):
    from app.api.mcp_policy import reset_rate_buckets_for_tests
    from app.api.mcp_user_secrets import _TEST_CONNECTION_RATE_LIMIT_PM

    reset_rate_buckets_for_tests()
    _seed_per_user_passthrough_tool()
    _store_analyst_secret(monkeypatch, "src_pu_pt", "tok")
    hdr = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}
    with patch("connectors.mcp.client.list_tools_async", new=AsyncMock(return_value=[])):
        for _ in range(_TEST_CONNECTION_RATE_LIMIT_PM):
            assert (
                seeded_app["client"].post("/api/mcp/sources/src_pu_pt/my-secret/test", headers=hdr).status_code == 200
            )
        r = seeded_app["client"].post("/api/mcp/sources/src_pu_pt/my-secret/test", headers=hdr)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    reset_rate_buckets_for_tests()
