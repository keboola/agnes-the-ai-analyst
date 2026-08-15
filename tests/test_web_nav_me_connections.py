"""The /me/connections page must be reachable from the user dropdown menu.

Shipped in #919 (per-user MCP connections self-service) but never wired into
the header — users could only reach it by typing the URL. Same placement
contract as the AI Connector entry (user dropdown, not primary nav).

The contract covers BOTH chromes. The original guard asserted only the
default topnav render, so the rail redesign shipped without the entry and
/me/connections went URL-only again on every ``AGNES_UI_LAYOUT=rail``
instance — the exact regression this file exists to prevent.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_connections_link_in_user_dropdown_for_non_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/me/connections"' in body
    assert ">My connections<" in body
    # User dropdown placement, not primary nav.
    assert "app-user-menu-item" in body


def test_connections_link_in_user_dropdown_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/dashboard", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert 'href="/me/connections"' in resp.text


def test_connections_link_in_rail_account_menu(seeded_app, monkeypatch):
    """Under the rail chrome the account menu is the page's ONLY entry point
    (the rail renders no topnav header, no command palette, and no global
    search), so the link must render there for every authenticated user."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    for token in (seeded_app["analyst_token"], seeded_app["admin_token"]):
        resp = c.get("/dashboard", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert 'href="/me/connections"' in body
        assert ">My connections<" in body
        # Account-menu placement (the rail reuses the topnav menu classes).
        assert "app-user-menu-item" in body
