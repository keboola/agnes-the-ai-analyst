"""The /me/connections page must be reachable from the user dropdown menu.

Shipped in #919 (per-user MCP connections self-service) but never wired into
the header — users could only reach it by typing the URL. Same placement
contract as the AI Connector entry (user dropdown, not primary nav).
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
