"""Global search box in the rail (t7).

Surfaces the existing unified search endpoint (GET /api/knowledge/search,
see app/api/knowledge_search.py) as a combobox in the shared chrome partial,
rendered for every authed dashboard-style page. These tests only assert the
static markup + script wiring — the fetch/debounce behaviour lives in
app/web/static/js/global_search.js and is exercised by browser-level checks,
not this DuckDB-backed suite.

The box shipped in the topnav chrome (`_app_header.html`) and moved to
`_app_rail.html` when that chrome was retired (Wave 0, 2026-08). The two ids
the script binds on — `#global-search`, `#globalSearchResults` — are the
contract and did not change; only the surrounding markup and the result rows'
classes did (`.rail-search-*`).
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestGlobalSearchHeader:
    def test_authed_page_renders_global_search_box(self, seeded_app):
        """Any authed page that includes the rail chrome gets the combobox."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/library", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert 'id="global-search"' in body
        assert 'role="combobox"' in body
        assert 'aria-expanded="false"' in body
        # Dropdown listbox target for the combobox.
        assert 'id="globalSearchResults"' in body
        assert 'role="listbox"' in body

    def test_authed_page_references_global_search_script(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/library", headers=_auth(token))
        assert resp.status_code == 200
        assert "/static/js/global_search.js" in resp.text

    def test_search_precedes_the_destinations_and_the_account_menu(self, seeded_app):
        """Order in the column: search, then the destinations, then the account
        menu at the foot.

        This replaces an assertion on the topnav's right-hand cluster (search →
        Admin mega-menu → user menu). Both halves of that premise are gone: the
        topnav chrome was retired in Wave 0 (2026-08), and the rail's Admin
        mega-menu was already replaced by a single `/admin` destination backed
        by `app/web/admin_nav.py`. What survives — and is worth pinning — is
        that search comes FIRST, above the destinations rather than buried in
        the account menu at the bottom, since it is the one control that is
        about the whole instance rather than a destination.
        """
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/library", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        search_pos = body.index('id="global-search"')
        admin_pos = body.index('href="/admin"')
        user_menu_pos = body.index('id="userMenu"')
        assert search_pos < admin_pos < user_menu_pos
        # Admin is a destination of its own, not a row in the personal account menu.
        assert "app-user-menu-admin" not in body

    def test_admin_destination_hidden_from_non_admin(self, seeded_app):
        """The Admin destination is admin-only. A plain analyst never sees it
        (backend still gates /admin/* independently).

        Pinned on the rail's actual admin entry rather than the retired
        mega-menu ids — asserting those absent would pass for every viewer now
        that no chrome emits them, which is a guard that cannot fail.
        """
        c = seeded_app["client"]
        analyst = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
        assert analyst.status_code == 200
        assert 'href="/admin"' not in analyst.text

        # The same page for an admin DOES carry it — without this half the
        # assertion above would also pass if the entry vanished for everyone.
        admin = c.get("/library", headers=_auth(seeded_app["admin_token"]))
        assert admin.status_code == 200
        assert 'href="/admin"' in admin.text

    def test_anonymous_login_page_has_no_search_box(self, seeded_app):
        """base_login.html never includes the rail partial (gated on
        `session.user`), so the box can't appear on an unauthenticated page."""
        c = seeded_app["client"]
        resp = c.get("/login")
        assert resp.status_code == 200
        assert 'id="global-search"' not in resp.text
