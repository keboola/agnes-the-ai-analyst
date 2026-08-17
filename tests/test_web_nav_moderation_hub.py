"""Admin menu: the "Moderation & Trust" hub (/admin/store) surfaces in the
Admin menu for admins and never for non-admins.

Deliberately UNGATED on `store.verification_enabled` (spec 2026-08-07,
accepted deviations — settled in Devin Review round 5 on #1200): the hub
hosts the flea submission-review count and marketplace-curation jump-offs
even with verification off, so hiding the row would unlink live content.
The page itself hides its verification section when the switch is off
(`admin_moderation_hub.html` renders off `store_verification_enabled`).

The link used to live in the topnav's Admin mega-menu, and this suite
rendered /dashboard to find it there. Wave 0 (2026-08) retired that chrome and
/dashboard with it; the admin inventory is `app/web/admin_nav.py`, rendered as
the admin sidebar on every /admin/* page, so that is where the row is asserted
now. `/admin/store` reaching the inventory at all was an open TODO on the old
hand-written rail menu — this is the guard that it stayed closed.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_moderation_hub_link_in_admin_menu_for_admin(seeded_app):
    """Present for admins regardless of the verification switch (off here —
    the default)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin", headers=_auth(token))
    assert resp.status_code == 200
    # Exact href (trailing quote) so it doesn't match /admin/store/submissions.
    assert 'href="/admin/store"' in resp.text


def test_moderation_hub_link_absent_for_non_admin(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    # A non-admin cannot reach /admin at all, and no ordinary page carries an
    # admin row — the sidebar renders only for admins on admin pages.
    assert c.get("/admin", headers=_auth(token)).status_code in (302, 303, 403)
    resp = c.get("/library", headers=_auth(token))
    assert resp.status_code == 200
    assert 'href="/admin/store"' not in resp.text
