"""The /me/connections self-service per-user credential page."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from src.db import get_system_db
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.user_groups import UserGroupsRepository


def _seed(analyst_id: str = "analyst1") -> None:
    """Granted per_user source (shows), ungranted per_user (hidden), granted
    shared (hidden — nothing personal to connect)."""
    conn = get_system_db()
    sources = MCPSourceRepository(conn)
    tools = ToolRegistryRepository(conn)
    groups = UserGroupsRepository(conn)
    members = UserGroupMembersRepository(conn)

    grp = groups.create(name="conn-grp", description=None)
    members.add_member(analyst_id, grp["id"], source="system_seed")

    # Granted per_user — must appear.
    sources.upsert(
        id="src_conn_pu",
        name="ConnGrantedPU",
        transport="http",
        url="https://up.example/mcp",
        scope="per_user",
        connect_hint="Generate a token in Settings.",
    )
    tools.upsert(
        tool_id="src_conn_pu.lookup",
        source_id="src_conn_pu",
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
    )
    tools.add_grant("src_conn_pu.lookup", grp["id"])

    # Ungranted per_user — must NOT appear.
    sources.upsert(
        id="src_conn_ung", name="ConnUngrantedPU", transport="http", url="https://up.example/mcp", scope="per_user"
    )
    tools.upsert(
        tool_id="src_conn_ung.lookup",
        source_id="src_conn_ung",
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
    )

    # Granted shared — must NOT appear (no personal credential to manage).
    sources.upsert(
        id="src_conn_shared", name="ConnSharedSrc", transport="stdio", command="/bin/true", args=[], scope="shared"
    )
    tools.upsert(
        tool_id="src_conn_shared.lookup",
        source_id="src_conn_shared",
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
    )
    tools.add_grant("src_conn_shared.lookup", grp["id"])
    conn.close()


def _get(seeded_app):
    return seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )


def test_connections_page_grant_filtered_and_styled(seeded_app):
    _seed()
    r = _get(seeded_app)
    assert r.status_code == 200, r.text
    html = r.text
    assert "/static/" in html  # chrome context wired → stylesheet href non-empty
    assert "My connections" in html  # hero rendered
    assert "ConnGrantedPU" in html  # granted per_user source shows
    assert "ConnUngrantedPU" not in html  # ungranted hidden
    assert "ConnSharedSrc" not in html  # shared never listed


def test_connect_hint_strips_dangerous_scheme(seeded_app):
    """connect_hint is rendered through render_safe — a javascript: scheme or an
    inline script is stripped, never clickable/executable."""
    _seed()
    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_conn_pu",
        name="ConnGrantedPU",
        transport="http",
        url="https://up.example/mcp",
        scope="per_user",
        connect_hint="[x](javascript:alert(1)) <script>alert(1)</script>",
    )
    conn.close()
    html = _get(seeded_app).text
    # No LIVE javascript anchor and no LIVE script tag reach the browser.
    # (render_safe leaves a javascript: markdown link as inert text and escapes
    # raw HTML — the dangerous *executable* forms must be absent.)
    assert 'href="javascript:' not in html
    assert "<script>alert(1)</script>" not in html
