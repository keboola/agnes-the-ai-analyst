"""User dropdown: "Learn how it works" link for all authenticated users.

The user account dropdown carries a "Learn how it works" link for every
authenticated user, replacing the former "AI Connector" menu item. It points
at ``/how-it-works`` — the consolidated orientation page that absorbed both
/home's product story and the standalone AI Connector page, so "learn how it
works" and "connect your tool" are two sections of one document rather than
two pages that had already drifted apart.

The label stays "Learn how it works" in the TOPNAV chrome (the default —
changing its visible text would change every existing instance's look); the
opt-in rail chrome carries the clearer "How {brand} works" as a nav row of its
own, asserted in tests/test_web_how_it_works.py.

``/me/ai-connector`` and the older ``/me/mcp`` / ``/me/cowork`` aliases now
302-redirect to ``/how-it-works#connect``; the connector content itself is
asserted in tests/test_web_how_it_works.py.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_learn_link_in_user_dropdown_for_non_admin(seeded_app):
    """Non-admin users see the "Learn how it works" link in the user dropdown."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/how-it-works"' in body
    assert ">Learn how it works<" in body
    # Must carry .app-user-menu-item (user dropdown), not .app-nav-link (primary nav).
    assert "app-user-menu-item" in body
    # The former "AI Connector" dropdown item is gone (its page is now a section).
    assert ">AI Connector<" not in body


def test_learn_link_in_user_dropdown_for_admin(seeded_app):
    """Admin users also see the "Learn how it works" link in the user dropdown."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/how-it-works"' in body
    assert ">Learn how it works<" in body
    assert ">AI Connector<" not in body
    # Cowork must NOT appear in the Admin dropdown or as a primary nav link.
    assert 'href="/me/mcp"' not in body


def test_learn_link_no_longer_points_at_the_install_wizard(seeded_app):
    """Regression guard for the bug this consolidation fixed.

    /home renders `home_not_onboarded.html` — a page titled "Setup" whose body
    is a CLI install wizard (`curl … | bash`, pick a folder, paste a token
    script). Sending a web user who clicked "Learn how it works" there was the
    single worst expectation break in the nav: the label promises an
    explanation and the page delivered a terminal procedure.
    """
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text
    assert '<a class="app-user-menu-item {% if' not in body  # sanity: rendered, not raw
    assert 'href="/home">Learn how it works<' not in body


@pytest.mark.parametrize("legacy", ["/me/ai-connector", "/me/mcp", "/me/cowork"])
def test_connector_urls_redirect_to_the_connect_section(seeded_app, legacy):
    """The standalone connector page and its aliases land on ``#connect``.

    302 rather than 301 on purpose: a permanent redirect is cached by the
    browser indefinitely and would be very hard to walk back if the
    consolidation is ever revisited.
    """
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get(legacy, headers=_auth(token), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/how-it-works#connect"


def test_connector_urls_still_reach_the_content_when_followed(seeded_app):
    """Following the redirect lands on a page that really has the connector.

    Guards the bookmark path end-to-end: anyone who saved /me/ai-connector
    keeps arriving at the connector URL box, not at a 404 or an explainer that
    only *mentions* connecting.
    """
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/me/ai-connector", headers=_auth(token), follow_redirects=True)
    assert resp.status_code == 200
    assert "/api/mcp/http" in resp.text
    assert "Connector URL" in resp.text
