"""``/me/connections`` and the admin inline "Your connection" panel —
oauth-source UI (2026-07-30 outbound MCP OAuth sources spec §3).

Two layers, mirroring ``tests/test_admin_mcp_ui_fields.py``'s style:

* Template-content assertions — cheap, deterministic checks that the
  templates carry the Connect/Disconnect markup and the connected/error
  banner hooks.
* A route-level integration test that a granted analyst sees the oauth
  branch rendered for an ``auth_method='oauth'`` source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

TPL = Path("app/web/templates")


def _read(name: str) -> str:
    return (TPL / name).read_text()


# ---------------------------------------------------------------------------
# Template content
# ---------------------------------------------------------------------------


def test_me_connections_has_oauth_connect_and_disconnect_markup():
    html = _read("me_connections.html")
    assert 'data-auth-kind="{{ s.auth_kind }}"' in html
    assert "oauth/authorize" in html
    assert 'data-action="oauth-disconnect"' in html
    assert "oauth/connection" in html  # DELETE endpoint used by JS


def test_me_connections_has_connected_and_error_banners():
    html = _read("me_connections.html")
    assert "connected_source" in html
    assert "connect_error" in html


def test_admin_detail_has_oauth_connect_and_disconnect_controls():
    html = _read("admin_mcp_source_detail.html")
    assert 'id="myconn-oauth-controls"' in html
    assert 'id="myconn-oauth-connect-link"' in html
    assert "myconn-oauth-disconnect-btn" in html
    assert "myconn-oauth-test-btn" in html
    assert "oauth/authorize" in html
    assert "oauth/connection" in html


# ---------------------------------------------------------------------------
# Route: GET /me/connections
# ---------------------------------------------------------------------------


def _seed_oauth_source(source_id: str = "src_oauth_ui", grant_to: str = "analyst1") -> None:
    from src.db import get_system_db
    from src.repositories.mcp_sources import MCPSourceRepository
    from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=source_id,
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    tools = ToolRegistryRepository(conn)
    tools.upsert(
        tool_id=f"{source_id}.lookup",
        source_id=source_id,
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        description="grant target",
    )
    grp = UserGroupsRepository(conn).create(name=f"grant-{source_id}", description=None)
    tools.add_grant(f"{source_id}.lookup", grp["id"])
    UserGroupMembersRepository(conn).add_member(grant_to, grp["id"], source="system_seed")
    conn.close()


def test_me_connections_page_renders_oauth_source(seeded_app):
    _seed_oauth_source()
    r = seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200, r.text
    assert 'data-auth-kind="oauth"' in r.text
    assert "/api/mcp/sources/src_oauth_ui/oauth/authorize" in r.text


def test_me_connections_page_shows_connected_banner(seeded_app):
    _seed_oauth_source()
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connected": "src_oauth_ui"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert "Connected src_oauth_ui" in r.text


def test_me_connections_page_shows_connect_error_banner(seeded_app):
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connect_error": "token exchange failed"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert "token exchange failed" in r.text
