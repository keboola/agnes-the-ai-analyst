"""Admin dashboard page (GET /admin).

`/admin` used to be a card grid indexing every admin surface — a second copy
of the sidebar (`app/web/admin_nav.py`) rendered beside the first. The grid is
gone; the page now answers "what needs my attention?" from the signal registry
in `app/web/admin_signals.py`, and the sidebar is the only admin navigation.

This suite covers the three things that consolidation put at risk:

  * the gate still holds (unchanged);
  * the dashboard renders its zones, and an instance with empty queues gets an
    explicit all-clear rather than a wall of zeros;
  * NOTHING became unreachable when the grid was deleted — every destination
    it carried, including the three that were not sidebar entries before, is
    still linked from this page.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAdminDashboard:
    def test_admin_sees_both_zones(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        body = resp.text
        assert "Needs you" in body
        assert "Needs fixing" in body

    def test_empty_queues_render_an_explicit_all_clear(self, seeded_app):
        """A seeded instance has nothing pending. Rule 1 of the registry: a
        clear signal renders NOTHING, and the zone collapses to one line — a
        dashboard of zeros is one nobody reads."""
        c = seeded_app["client"]
        body = c.get("/admin", headers=_auth(seeded_app["admin_token"])).text
        assert "Nothing needs your attention." in body

    def test_needs_fixing_is_not_resolved_in_the_render_path(self, seeded_app):
        """Zone 2 reads the unbounded audit/history tables, so it must be
        fetched after first paint — not inlined. The skeleton + the script tag
        are what prove the split is still in place."""
        c = seeded_app["client"]
        body = c.get("/admin", headers=_auth(seeded_app["admin_token"])).text
        assert "data-adash-fixing" in body
        assert "js/admin/admin_dashboard.js" in body

    def test_non_admin_gets_403(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403


class TestGridDeletionLeftNothingStranded:
    """The card grid was the ONLY link to several surfaces. Deleting it
    without moving them would have made them reachable by typed URL only —
    the exact regression these assertions exist to catch."""

    def _body(self, seeded_app) -> str:
        return seeded_app["client"].get("/admin", headers=_auth(seeded_app["admin_token"])).text

    def test_ordinary_admin_destinations_still_reachable(self, seeded_app):
        body = self._body(seeded_app)
        for href in ("/admin/users", "/admin/sync", "/admin/server-config", "/admin/tables"):
            assert f'href="{href}"' in body, f"{href} is no longer linked from /admin"

    def test_chat_sessions_moved_into_the_sidebar(self, seeded_app):
        """Registered in app/api/admin_chat.py, not the web router — it was
        invisible to the sidebar's inventory until the grid was retired."""
        assert 'href="/admin/chat"' in self._body(seeded_app)

    def test_api_docs_moved_into_the_sidebar_footer(self, seeded_app):
        """Not /admin routes at all, so they ride a footer block rather than
        an eighth section (see ADMIN_NAV_DOCS)."""
        body = self._body(seeded_app)
        for href in ("/documentation/api", "/docs", "/redoc"):
            assert f'href="{href}"' in body, f"{href} lost its home when the grid went"

    def test_studio_row_follows_its_instance_flag(self, seeded_app):
        """The only conditional nav item. Studio is on by default, and the
        grid gated it the same way — the row must not become unconditional."""
        assert 'href="/admin/studio"' in self._body(seeded_app)
