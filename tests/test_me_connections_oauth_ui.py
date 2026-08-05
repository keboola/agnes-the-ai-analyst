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

from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    """Token upserts in these tests encrypt through the vault — same
    autouse key fixture as tests/test_mcp_user_secrets.py."""
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


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
    from src.repositories import mcp_user_oauth_tokens_repo

    _seed_oauth_source()
    mcp_user_oauth_tokens_repo().upsert("src_oauth_ui", "analyst1", "atok")
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connected": "src_oauth_ui"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert "Connected src_oauth_ui" in r.text


def test_me_connections_connected_banner_shows_for_source_without_tools(seeded_app):
    """The banner check keys off the caller's stored token row, not the
    page's tool-derived source list — a freshly registered source has no
    tools yet, and the admin's post-connect banner must still show (Devin
    Review on #1130)."""
    from src.db import get_system_db
    from src.repositories import mcp_user_oauth_tokens_repo
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_oauth_fresh",
        name="src_oauth_fresh",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()
    mcp_user_oauth_tokens_repo().upsert("src_oauth_fresh", "analyst1", "atok")
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connected": "src_oauth_fresh"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert "Connected src_oauth_fresh" in r.text


def test_me_connections_lapsed_connection_still_offers_disconnect(seeded_app):
    """An expired token with no refresh path is not usable (no green pill),
    but the stored row must still be removable from the page (Devin Review
    on #1130)."""
    from datetime import datetime, timedelta, timezone

    from src.repositories import mcp_user_oauth_tokens_repo

    _seed_oauth_source(source_id="src_oauth_lapsed")
    mcp_user_oauth_tokens_repo().upsert(
        "src_oauth_lapsed",
        "analyst1",
        "atok",
        refresh_token=None,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    r = seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    section = r.text.split('id="source-src_oauth_lapsed"')[1]
    section = section.split('class="conn-card"')[0]  # just this card
    assert "Expired — reconnect" in section
    assert 'data-action="oauth-disconnect"' in section
    assert ">Reconnect<" in section


def test_me_connections_page_shows_connect_error_banner(seeded_app):
    from app.api.mcp_oauth_connect import CONNECT_ERROR_MESSAGES

    r = seeded_app["client"].get(
        "/me/connections",
        params={"connect_error": "token_exchange_failed"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert CONNECT_ERROR_MESSAGES["token_exchange_failed"] in r.text


def test_me_connections_error_banner_never_echoes_crafted_text(seeded_app):
    """?connect_error= is reachable via a hand-crafted link, so anything not
    in the fixed code→message map must render the generic fallback — never
    the link's own words inside an Agnes-branded banner (Devin Review on
    #1130)."""
    from app.api.mcp_oauth_connect import CONNECT_ERROR_FALLBACK

    crafted = "your account was suspended, call +1-555-0100"
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connect_error": crafted},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert crafted not in r.text
    assert "suspended" not in r.text
    assert CONNECT_ERROR_FALLBACK in r.text


def test_me_connections_connected_banner_only_names_visible_sources(seeded_app):
    """Same crafted-link channel as connect_error: ?connected= must only
    ever name a source the caller can actually see."""
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connected": "definitely-not-a-source"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert "definitely-not-a-source" not in r.text
