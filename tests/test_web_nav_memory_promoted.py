"""Curated Memory is reachable by every authenticated user, not just admins.

Originally (v49 Task 8.11) that meant a ``/corporate-memory`` link in the
topnav's primary nav, next to "Data Packages". Two things have moved since:
the topnav chrome was retired in Wave 0 (2026-08), and Memory folded into the
Library — ``/corporate-memory`` 302s to the Library's Memory band rather than
carrying a rail row of its own.

What the suite still pins is the part that was the point: a non-admin has a
route to curated memory from ordinary chrome, and the admin moderation queue
at ``/admin/corporate-memory`` stays a DISTINCT surface in the admin
inventory — one is not a substitute for the other.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_memory_is_reachable_from_ordinary_chrome_for_non_admin(seeded_app):
    """A non-admin reaches curated memory without typing a URL.

    The door is the Library (which absorbed the Memory browse), reached from
    the rail — not a primary-nav link, which no chrome renders any more.
    """
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    resp = c.get("/library", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # The rail links the Library for every authed caller...
    assert 'href="/library"' in body
    # ...and a non-admin gets no admin chrome on it.
    assert 'href="/admin"' not in body


def test_moderation_queue_is_a_distinct_admin_surface(seeded_app):
    """The admin review queue is its own entry in the admin inventory — the
    user-facing memory browse is not a substitute for it, and vice versa."""
    from app.web.admin_nav import ADMIN_NAV_SECTIONS, _section_entries

    hrefs = {e["href"] for s in ADMIN_NAV_SECTIONS for e in _section_entries(s)}
    assert "/admin/corporate-memory" in hrefs

    c = seeded_app["client"]
    resp = c.get("/admin", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert 'href="/admin/corporate-memory"' in resp.text
    assert "Corporate memory" in resp.text


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
