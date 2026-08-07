"""Primary nav: the "Moderation & Trust" hub (/admin/store) surfaces in the
Admin menu for admins and never for non-admins.

Renders the default topnav chrome (no AGNES_UI_LAYOUT) — the default look the
design-system contract forbids changing — so this proves the additive
mega-menu entry, not the rail collapse (that is guarded in
test_rail_journey_chrome.py).
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_moderation_hub_link_in_admin_menu_for_admin(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    # Exact href (trailing quote) so it doesn't match /admin/store/submissions.
    assert 'href="/admin/store"' in resp.text


def test_moderation_hub_link_absent_for_non_admin(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    # The admin menu isn't rendered for a non-admin, so the hub link is absent.
    assert 'href="/admin/store"' not in resp.text
