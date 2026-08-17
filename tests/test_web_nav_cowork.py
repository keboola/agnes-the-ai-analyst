"""AI Connector routing (spec 2026-08-07; Wave 0, 2026-08 legacy retirement).

The consolidation: ``/me/ai-connector`` 302s unconditionally to
``/how-it-works#connect`` (the orientation page that absorbed both /home's
product story and the standalone connector page). The older ``/me/mcp`` /
``/me/cowork`` aliases 302 onto ``/me/ai-connector``.

The topnav chrome's own pre-redesign contract — an "AI Connector" item in the
user dropdown pointing at a real standalone page (``me_cowork_legacy.html``)
— was deleted in Task 2/4 of Wave 0 along with ``_app_header.html``; rail is
the only chrome now, and its own nav carries a "How {brand} works" row
instead, asserted in tests/test_web_how_it_works.py.

302 rather than 301 everywhere on purpose: a permanent redirect is cached by
the browser indefinitely and would be very hard to walk back if the routing
is revisited.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("legacy", ["/me/mcp", "/me/cowork"])
def test_alias_urls_redirect_to_ai_connector(seeded_app, legacy):
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get(legacy, headers=_auth(token), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/me/ai-connector"


def test_connector_urls_land_on_the_connect_section(seeded_app):
    """/me/ai-connector forwards to /how-it-works#connect (aliases hop
    through /me/ai-connector)."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/me/ai-connector", headers=_auth(token), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/how-it-works#connect"


def test_connector_urls_still_reach_the_content_when_followed(seeded_app):
    """Following the bookmark path end-to-end lands on real connector
    content: anyone who saved /me/ai-connector keeps arriving at the
    connector URL box, not at a 404 or an explainer that only *mentions*
    connecting."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/me/ai-connector", headers=_auth(token), follow_redirects=True)
    assert resp.status_code == 200
    assert "/api/mcp/http" in resp.text
    assert "Connector URL" in resp.text
