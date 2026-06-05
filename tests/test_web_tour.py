"""Walking guide / product tour overlay.

The tour ships on every authed page via `_tour.html` (included by both base
layouts). It walks the primary nav, so its markup + assets must render for
signed-in users and stay out of the way otherwise. The engine is
client-side; these tests guard the server-rendered scaffolding the engine
binds to.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_tour_overlay_renders_on_authed_page(seeded_app):
    """An authed page carries the tour overlay root, the CSS, and the JS."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text

    assert 'id="agnesTour"' in body
    assert "css/tour.css" in body
    assert "js/tour.js" in body


def test_tour_anchors_present_on_nav(seeded_app):
    """The nav exposes the data-tour anchors the engine spotlights, plus the
    manual "Take a tour" launcher in the user menu."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text

    for key in ("nav-home", "nav-marketplace", "nav-catalog", "nav-memory", "user-menu"):
        assert f'data-tour="{key}"' in body, f"missing data-tour anchor: {key}"
    assert "data-tour-start" in body
    assert ">Take a tour<" in body


def test_tour_not_rendered_when_unauthenticated(seeded_app):
    """No session → no tour. The overlay is guarded by `session.user`, so an
    anonymous request (which redirects to login) never ships the overlay."""
    c = seeded_app["client"]
    resp = c.get("/login")
    assert "id=\"agnesTour\"" not in resp.text
