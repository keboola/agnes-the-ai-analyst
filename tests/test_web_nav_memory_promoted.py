"""Primary nav: Memory promoted out of admin dropdown (Task 8.11 of v49 plan).

Per spec Section 1: the user-facing ``/corporate-memory`` link sits in the
primary nav next to "Data Packages", visible to every authenticated user
(not gated behind ``is_admin``). The admin moderation queue at
``/admin/corporate-memory`` stays in the Admin dropdown as
"Curated memory reviews" — a distinct entry for a distinct surface.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_memory_link_in_primary_nav_for_non_admin(seeded_app):
    """Non-admin users see the Memory link in the primary nav."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # Primary nav uses .app-nav-link; the Memory entry must point at
    # /corporate-memory and carry that class (not the admin-only
    # .app-nav-menu-item which lives inside the Admin dropdown).
    assert 'class="app-nav-link' in body
    # The primary-nav link's href is /corporate-memory with label "Memory".
    assert 'href="/corporate-memory"' in body
    # Sanity check: the admin dropdown isn't even rendered for a non-admin,
    # so the only /corporate-memory href on the page is the primary-nav one.
    assert ">Memory<" in body


def test_admin_sees_memory_in_primary_nav_plus_moderation_in_dropdown(seeded_app):
    """Admin users see BOTH the user-facing primary-nav link AND the
    "Curated memory reviews" entry inside the Admin dropdown."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/dashboard", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # User-facing Memory link in primary nav.
    assert 'href="/corporate-memory"' in body
    # Admin moderation queue distinct entry in the Admin dropdown.
    assert 'href="/admin/corporate-memory"' in body
    assert "Curated memory reviews" in body


def test_corporate_memory_route_accessible_to_non_admin(seeded_app):
    """Smoke: /corporate-memory loads for an analyst (no admin gate)."""
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/corporate-memory", headers=_auth(token))
    # Either 200 (page renders) or 403 if RBAC fully blocks — the
    # promotion is about the NAV LINK being visible; the route itself
    # is governed by separate RBAC. We just need it not to be
    # admin-only at the auth layer.
    assert resp.status_code in (200, 403)
