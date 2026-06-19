"""Primary nav: AI Cowork in primary nav for all authenticated users.

The /me/ai-connector page is user-facing (bundle setup, tools reference) and must
be reachable from the primary nav for every authenticated user, not gated
behind the admin-only Admin dropdown. The legacy /me/mcp URL 301-redirects
to /me/ai-connector.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_cowork_link_in_user_dropdown_for_non_admin(seeded_app):
    """Non-admin users see the AI Cowork link in the user dropdown menu."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/me/ai-connector"' in body
    assert ">AI Cowork<" in body
    # Must carry .app-user-menu-item (user dropdown), not .app-nav-link (primary nav).
    assert "app-user-menu-item" in body


def test_cowork_link_in_user_dropdown_for_admin(seeded_app):
    """Admin users also see the AI Cowork link in the user dropdown menu."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/me/ai-connector"' in body
    assert ">AI Cowork<" in body
    # Cowork must NOT appear in the Admin dropdown or as a primary nav link.
    assert 'href="/me/mcp"' not in body


def test_me_mcp_redirects_to_me_cowork(seeded_app):
    """Legacy /me/mcp 301-redirects to /me/ai-connector."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/me/mcp", headers=_auth(token), follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/me/ai-connector"


def test_me_cowork_accessible_to_non_admin(seeded_app):
    """Smoke: /me/ai-connector loads for a non-admin user."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/me/ai-connector", headers=_auth(token))
    assert resp.status_code == 200
    assert "AI Cowork" in resp.text


def test_me_cowork_has_plugin_package_section(seeded_app):
    """/me/ai-connector hosts the per-plugin download list + the package guideline.

    The list used to live on /home; it was relocated here so there is a single
    place for the "what is a package" explanation. Pin: the JS-populated
    download container, the per-plugin Cowork endpoint the JS builds links
    against, and the guideline copy are all present."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    body = c.get("/me/ai-connector", headers=_auth(token)).text
    assert 'id="cowork-plugin-list"' in body
    assert "/marketplace/cowork/" in body
    assert "Plugin packages" in body


def test_me_cowork_shows_oauth_connector_url(seeded_app):
    """The Connection section surfaces the OAuth 2.1 connector URL
    (`/api/mcp/http`) as the endpoint users paste into a remote MCP client.
    Without this the no-token connector path is undiscoverable in the UI.
    """
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    body = c.get("/me/ai-connector", headers=_auth(token)).text
    assert "/api/mcp/http" in body
    assert "Connector URL" in body
