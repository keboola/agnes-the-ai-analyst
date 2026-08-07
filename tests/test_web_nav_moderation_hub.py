"""Admin menu: the "Moderation & Trust" hub (/admin/store) is gated on the
Store verification workflow (spec 2026-08-07 wave 2).

Semantic gate, not a chrome check: the hub exists to moderate the
verification workflow, and with ``store.verification_enabled`` off (the
default) it has nothing to moderate — so a default instance's admin menu
stays byte-for-byte pre-redesign. Enabling verification surfaces the row for
admins; non-admins never see it (the admin menu doesn't render for them).

Renders the default topnav chrome (no AGNES_UI_LAYOUT) — the rail collapse is
guarded in test_rail_journey_chrome.py.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_moderation_hub_link_absent_by_default(seeded_app):
    """Verification off (the default) → no hub row, even for admins."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    # Exact href (trailing quote) so it doesn't match /admin/store/submissions.
    assert 'href="/admin/store"' not in resp.text


def test_moderation_hub_link_in_admin_menu_when_verification_on(seeded_app, monkeypatch):
    import app.instance_config as ic

    monkeypatch.setattr(
        ic,
        "get_value",
        lambda *keys, default=None: True if keys == ("store", "verification_enabled") else default,
    )
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    assert 'href="/admin/store"' in resp.text


def test_moderation_hub_link_absent_for_non_admin(seeded_app, monkeypatch):
    import app.instance_config as ic

    monkeypatch.setattr(
        ic,
        "get_value",
        lambda *keys, default=None: True if keys == ("store", "verification_enabled") else default,
    )
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    # The admin menu isn't rendered for a non-admin, so the hub link is absent
    # even with verification enabled.
    assert 'href="/admin/store"' not in resp.text
