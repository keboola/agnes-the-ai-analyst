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
    (?) help icon launcher in the header."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text

    for key in ("nav-home", "nav-marketplace", "nav-catalog", "nav-memory", "user-menu"):
        assert f'data-tour="{key}"' in body, f"missing data-tour anchor: {key}"
    assert "data-tour-start" in body


def test_tour_not_rendered_when_unauthenticated(seeded_app):
    """No session → no tour. The overlay is guarded by `session.user`, so an
    anonymous request (which redirects to login) never ships the overlay."""
    c = seeded_app["client"]
    resp = c.get("/login")
    assert "id=\"agnesTour\"" not in resp.text


def test_intro_modal_and_injected_steps_present(seeded_app):
    """First-visit consent modal + the server-injected step JSON are rendered
    (the engine reads the steps from the page, not a hardcoded array)."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text

    assert 'id="agnesOnboardingIntro"' in body
    assert "Show me around" in body
    assert 'id="agnesOnboardingSteps"' in body
    # The injected JSON carries the non-admin steps.
    assert '"key": "home"' in body or '"key":"home"' in body


def test_card_chrome_renders(seeded_app):
    """The polished card chrome — progress bar, the icon slot, and the step
    header — must ship so the engine has elements to populate."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text

    assert "agnes-tour__bar-fill" in body, "progress bar missing from the card"
    assert "agnes-tour__icon" in body, "step icon slot missing from the card"
    assert "agnes-tour__head" in body
    assert "agnes-tour__tips" in body, "per-step tips list missing from the card"


def test_injected_steps_carry_tips(seeded_app):
    """The guide's substance — the per-step bullets — must reach the browser
    in the injected JSON, not just the title/body."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text
    assert '"tips"' in body, "steps JSON must carry the tips bullets"


def test_injected_steps_carry_route_and_icon(seeded_app):
    """Cross-page walk + wayfinding glyph both depend on the server emitting
    `route` and `icon` on each step in the injected JSON."""
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text

    assert '"route"' in body, "steps JSON must carry a route for cross-page navigation"
    assert '"icon"' in body, "steps JSON must carry an icon"


def test_injected_steps_are_role_split(seeded_app):
    """Admin sees the admin-only step in the injected JSON; non-admin doesn't.
    Proves the audience split happens server-side, before the browser."""
    c = seeded_app["client"]
    analyst = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text
    admin = c.get("/dashboard", headers=_auth(seeded_app["admin_token"])).text

    # The admin-only step keys off the nav-admin anchor.
    assert "nav-admin" in admin, "admin should receive the admin onboarding step"
    assert "nav-admin" not in analyst, "non-admin must not receive the admin step"
