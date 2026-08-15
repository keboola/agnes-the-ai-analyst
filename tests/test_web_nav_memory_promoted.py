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

from tests.test_web_memory_unified import _grant, _make_domain


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_non_admin_reaches_granted_curated_memory(seeded_app):
    """A non-admin actually gets to the curated memory they were granted.

    The door is the Library, which absorbed the Memory browse — /corporate-memory
    302s into its Memory band. Both halves are asserted: that the redirect is a
    redirect and not an admin gate, and that the granted domain is really on the
    page it lands on.

    An earlier revision of this test asserted `href="/library"` in the body and
    `href="/admin"` absent. Both were true of every authed page and neither
    mentioned memory, so it could not fail for the reason the file exists.
    """
    dom = _make_domain(slug="nav-mem", name="Nav Memory")
    _grant("Everyone", dom, requirement="required", users=["analyst1"])

    c = seeded_app["client"]
    token = seeded_app["analyst_token"]

    hop = c.get("/corporate-memory", headers=_auth(token), follow_redirects=False)
    assert hop.status_code == 302, f"non-admin got {hop.status_code}, not a redirect into the Library"
    assert hop.headers["location"] == "/library?section=memory_domain"

    landing = c.get(hop.headers["location"], headers=_auth(token))
    assert landing.status_code == 200
    assert "Nav Memory" in landing.text, "the granted domain is not on the page the redirect lands on"


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
    resp = c.get("/corporate-memory", headers=_auth(token), follow_redirects=False)
    # 200 (renders) or 302 (folds into the Library band) — never 403. The
    # route is not admin-gated, which is the whole point of this file; the
    # old assertion allowed 403 and so tolerated the exact regression it was
    # written to catch.
    assert resp.status_code in (200, 302), resp.status_code
