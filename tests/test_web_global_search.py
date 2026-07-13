"""Global search box in the header (t7).

Surfaces the existing unified search endpoint (GET /api/knowledge/search,
see app/api/knowledge_search.py) as a combobox in the shared header partial
(_app_header.html), rendered for every authed dashboard-style page. These
tests only assert the static markup + script wiring — the fetch/debounce
behaviour lives in app/web/static/js/global_search.js and is exercised by
browser-level checks, not this DuckDB-backed suite.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestGlobalSearchHeader:
    def test_authed_page_renders_global_search_box(self, seeded_app):
        """Any authed page that includes _app_header.html gets the combobox."""
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

    def test_search_box_placed_before_admin_and_user_menu(self, seeded_app):
        """Right of the primary nav links, before the Admin dropdown / user
        area — assert relative document order for an admin viewer (who sees
        every element the markup can render)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/library", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        search_pos = body.index('id="global-search"')
        admin_menu_pos = body.index('id="adminNavMenu"')
        user_menu_pos = body.index('id="userMenu"')
        assert search_pos < admin_menu_pos
        assert search_pos < user_menu_pos

    def test_anonymous_login_page_has_no_search_box(self, seeded_app):
        """base_login.html never includes _app_header.html (gated on
        `session.user`), so the box can't appear on an unauthenticated page."""
        c = seeded_app["client"]
        resp = c.get("/login")
        assert resp.status_code == 200
        assert 'id="global-search"' not in resp.text
