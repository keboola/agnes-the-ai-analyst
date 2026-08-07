"""User dropdown + AI Connector routing — chrome-dependent (spec 2026-08-07).

The default chrome keeps the pre-redesign contract: an "AI Connector" item in
the user dropdown pointing at ``/me/ai-connector``, which renders the frozen
standalone page (``me_cowork_legacy.html``) with the Connector URL box and
the ``/mcp-connect`` token fallback. The older ``/me/mcp`` / ``/me/cowork``
aliases 302 onto ``/me/ai-connector``.

Under the redesign opt-in the consolidation stands: the dropdown row reads
"Learn how it works" → ``/how-it-works`` (the orientation page that absorbed
both /home's product story and the standalone connector page), and
``/me/ai-connector`` 302s to ``/how-it-works#connect``. Within the topnav
header the reachable opt-in look is the paper theme; the rail chrome has its
own nav ("How {brand} works" row, asserted in tests/test_web_how_it_works.py).

302 rather than 301 everywhere on purpose: a permanent redirect is cached by
the browser indefinitely and would be very hard to walk back if the routing
is revisited.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_ai_connector_link_in_user_dropdown_for_non_admin(seeded_app):
    """Default chrome: non-admins see the pre-redesign "AI Connector" row."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/me/ai-connector"' in body
    assert ">AI Connector<" in body
    # Must carry .app-user-menu-item (user dropdown), not .app-nav-link (primary nav).
    assert "app-user-menu-item" in body
    # The redesign wording stays behind the opt-in.
    assert ">Learn how it works<" not in body


def test_ai_connector_link_in_user_dropdown_for_admin(seeded_app):
    """Default chrome: admins see the same "AI Connector" dropdown row."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/me/ai-connector"' in body
    assert ">AI Connector<" in body
    assert ">Learn how it works<" not in body
    # Cowork must NOT appear in the Admin dropdown or as a primary nav link.
    assert 'href="/me/mcp"' not in body


def test_paper_user_dropdown_keeps_the_learn_link(seeded_app, monkeypatch):
    """Opt-in look (paper-on-topnav): the redesign wording stands, pointing at
    the consolidated /how-it-works page — never at the /home install wizard
    (the expectation break the consolidation fixed: a label promising an
    explanation must not deliver a CLI install procedure)."""
    monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text
    assert 'href="/how-it-works"' in body
    assert ">Learn how it works<" in body
    assert ">AI Connector<" not in body
    assert 'href="/home">Learn how it works<' not in body


def test_default_ai_connector_renders_the_standalone_page(seeded_app):
    """Default chrome: /me/ai-connector is a real page again — the frozen
    pre-redesign copy with the Connector URL box and the /mcp-connect token
    fallback link."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/me/ai-connector", headers=_auth(token), follow_redirects=False)
    assert resp.status_code == 200
    assert "/api/mcp/http" in resp.text
    assert "Connector URL" in resp.text
    assert "/mcp-connect" in resp.text


@pytest.mark.parametrize("legacy", ["/me/mcp", "/me/cowork"])
def test_alias_urls_redirect_to_ai_connector(seeded_app, legacy):
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get(legacy, headers=_auth(token), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/me/ai-connector"


def test_rail_connector_urls_land_on_the_connect_section(seeded_app, monkeypatch):
    """Redesign opt-in: the consolidation stands — /me/ai-connector forwards
    to /how-it-works#connect (aliases hop through /me/ai-connector)."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/me/ai-connector", headers=_auth(token), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/how-it-works#connect"


def test_connector_urls_still_reach_the_content_when_followed(seeded_app):
    """Following the bookmark path end-to-end lands on real connector content
    in BOTH worlds: anyone who saved /me/ai-connector keeps arriving at the
    connector URL box, not at a 404 or an explainer that only *mentions*
    connecting."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/me/ai-connector", headers=_auth(token), follow_redirects=True)
    assert resp.status_code == 200
    assert "/api/mcp/http" in resp.text
    assert "Connector URL" in resp.text
