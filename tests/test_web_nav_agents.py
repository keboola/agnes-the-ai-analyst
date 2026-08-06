"""Inbound-link guards for two pages that shipped with zero entry points.

`/agents` (the agent-profile builder) and `/mcp-connect` (token-based editor
setup) were both reachable only by typing the URL — same bug class as #919's
`/me/connections` (see `tests/test_web_nav_me_connections.py`). Placement
contract, mirroring the AI Connector / My connections entries:

- `/agents`      → user dropdown ("My agents"); it is a per-user resource
                   list, not instance administration, so it does NOT belong in
                   the primary nav or the Admin mega-menu.
- `/mcp-connect` → contextual link from the AI Connector page, which owns the
                   "connect an AI client" job. OAuth is the happy path there;
                   the token page is the fallback, so it gets a cross-link
                   rather than a second near-duplicate nav entry.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_agents_link_in_user_dropdown_for_non_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/agents"' in body
    assert ">My agents<" in body
    # User dropdown placement, not primary nav.
    assert "app-user-menu-item" in body


def test_agents_link_in_user_dropdown_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/dashboard", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert 'href="/agents"' in resp.text


def test_agents_link_hidden_when_agent_profiles_disabled(seeded_app, monkeypatch):
    """`can_agent_profiles` (get_agent_profiles_enabled()) gates the same nav
    entry point — mirrors test_web_studio.py's test_studio_nav_hidden_when_disabled."""
    monkeypatch.setattr("app.web.router.get_agent_profiles_enabled", lambda: False)
    c = seeded_app["client"]
    resp = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'href="/agents"' not in resp.text
    assert "href: '/agents'" not in resp.text  # command palette row too


def test_mcp_connect_linked_from_ai_connector_page(seeded_app):
    """The AI Connector page is the only inbound link to /mcp-connect — if this
    fails, the token setup page is unreachable again."""
    c = seeded_app["client"]
    resp = c.get("/me/ai-connector", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'href="/mcp-connect"' in resp.text


def test_news_link_in_user_dropdown_for_non_admin(seeded_app):
    """`/news`'s other two entry points are both conditional: /home's "What's
    new" strip needs a published version AND `home_route == '/home'`, and the
    command palette bails out unless `#adminMenu` is in the DOM. On the
    `/dashboard` default that left a non-admin unable to reach the page at all
    (Devin Review on #1159), so it gets a dropdown entry like the others."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text
    assert 'href="/news">News</a>' in body


def test_command_palette_is_admin_only_so_it_cannot_be_the_entry_point(seeded_app):
    """Pins WHY the dropdown entries above have to exist.

    The palette is a convenience for admins, not a reachability guarantee: its
    IIFE returns immediately when `#adminMenu` is absent. Asserting the palette
    rows alone would pass for a non-admin — the `<script>` body is emitted for
    everyone — while the surface never initializes, which is false assurance of
    exactly the property these tests exist to defend."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text
    # the rows ship to everyone …
    assert "href: '/agents'" in body
    assert "href: '/mcp-connect'" in body
    assert "href: '/news'" in body
    # … behind a gate a non-admin never passes.
    assert "if (!document.getElementById('adminMenu')) return;" in body
    assert 'id="adminMenu"' not in body, "non-admin page must not carry the admin menu"
