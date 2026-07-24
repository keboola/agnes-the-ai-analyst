"""User dropdown: "Learn how it works" link for all authenticated users.

The user account dropdown carries a "Learn how it works" link (→ /home) for
every authenticated user, replacing the former "AI Connector" menu item —
the AI Connector page is now reached from the "Connect your tools" CTA on the
chat dashboard's tools card. The /me/ai-connector page itself stays
user-facing (bundle setup, tools reference); the legacy /me/mcp URL
301-redirects to it.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_learn_link_in_user_dropdown_for_non_admin(seeded_app):
    """Non-admin users see the "Learn how it works" link in the user dropdown."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/home"' in body
    assert ">Learn how it works<" in body
    # Must carry .app-user-menu-item (user dropdown), not .app-nav-link (primary nav).
    assert "app-user-menu-item" in body
    # The former "AI Connector" dropdown item is gone (moved to the tools card CTA).
    assert ">AI Connector<" not in body


def test_learn_link_in_user_dropdown_for_admin(seeded_app):
    """Admin users also see the "Learn how it works" link in the user dropdown."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/home"' in body
    assert ">Learn how it works<" in body
    assert ">AI Connector<" not in body
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
    assert "AI Connector" in resp.text


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


def test_me_cowork_has_claude_code_setup_guide(seeded_app):
    """The AI Connector guide carries a Claude Code panel: the `claude mcp
    add` CLI one-liner, the restart-before-it-appears note, and the SSE
    fallback recipe for older servers that don't serve the streamable
    endpoint.
    """
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    body = c.get("/me/ai-connector", headers=_auth(token)).text
    assert 'data-panel="claude-code"' in body
    assert "claude mcp add --scope user --transport http agnes" in body
    assert "--transport sse agnes" in body  # SSE fallback recipe
    assert "/api/mcp/sse" in body
    assert "only appears after restarting Claude Code" in body
