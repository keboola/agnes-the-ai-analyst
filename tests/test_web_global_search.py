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

    def test_actions_order_search_then_admin_then_user_menu(self, seeded_app):
        """In the single-row header the right cluster runs search → Admin →
        user menu. Admin is a FIRST-CLASS trigger (its own mega-menu), no
        longer buried in the user-profile dropdown. Assert for an admin viewer,
        who renders every element."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/library", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        search_pos = body.index('id="global-search"')
        admin_trigger_pos = body.index('id="adminMenuTrigger"')
        admin_panel_pos = body.index('id="adminMenuPanel"')
        user_menu_pos = body.index('id="userMenu"')
        user_panel_pos = body.index('id="userMenuPanel"')
        admin_link_pos = body.index('href="/admin/users"')
        # Right cluster order: search, then Admin, then the user menu.
        assert search_pos < admin_trigger_pos < user_menu_pos
        # Admin links live in the admin mega-menu panel, not the user dropdown.
        assert admin_panel_pos < admin_link_pos < user_panel_pos
        # Admin is out of the personal account menu entirely.
        assert "app-user-menu-admin" not in body
        # The mega-menu links to the /admin hub page.
        assert 'href="/admin"' in body

    def test_admin_surfaces_hidden_from_non_admin(self, seeded_app):
        """The Admin trigger + mega-menu are admin-only. A plain analyst never
        sees them (backend still gates /admin/* independently)."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/library", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert 'id="adminMenuTrigger"' not in body
        assert 'href="/admin/users"' not in body

    def test_anonymous_login_page_has_no_search_box(self, seeded_app):
        """base_login.html never includes _app_header.html (gated on
        `session.user`), so the box can't appear on an unauthenticated page."""
        c = seeded_app["client"]
        resp = c.get("/login")
        assert resp.status_code == 200
        assert 'id="global-search"' not in resp.text
