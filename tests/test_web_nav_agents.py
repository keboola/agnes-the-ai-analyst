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


def test_mcp_connect_linked_from_ai_connector_page(seeded_app):
    """The AI Connector page is the only inbound link to /mcp-connect — if this
    fails, the token setup page is unreachable again."""
    c = seeded_app["client"]
    resp = c.get("/me/ai-connector", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'href="/mcp-connect"' in resp.text


def test_command_palette_carries_both_pages(seeded_app):
    """Secondary entry point: both pages are listed in the Cmd/Ctrl-K palette
    under the user-facing group."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text
    assert "href: '/agents'" in body
    assert "href: '/mcp-connect'" in body


def test_command_palette_carries_news(seeded_app):
    """`/news` is otherwise only linked from /home's "What's new" strip, which
    needs both a published version and `home_route == '/home'` — on the
    `/dashboard` default that left the page with no non-admin entry point."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text
    assert "href: '/news'" in body
