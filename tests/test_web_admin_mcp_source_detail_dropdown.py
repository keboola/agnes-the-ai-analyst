"""Custom design-system dropdown on the MCP source detail edit form (#1055).

Each of the edit form's three `<select>`s — transport, secret scope, auth
method — stays a real `<select>` in the DOM (existing JS wiring:
`syncTransportFields` / `edit-toggle-btn` / `edit-save-btn` untouched) with a
`ds.dropdown()` custom button+menu alongside it. Visibility between the two
is a CSS theme decision, not a template one — see
`app/web/static/css/paper-skin.css`.
"""

from __future__ import annotations


def _auth(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


class TestMcpSourceDetailEditFormDropdowns:
    def test_native_selects_still_render_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/admin/mcp-sources/does-not-exist", headers=_auth(seeded_app))
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="edit-transport" class="ds-dropdown-native">' in text
        assert '<select id="edit-scope" class="ds-dropdown-native">' in text
        assert '<select id="edit-auth-method" class="ds-dropdown-native">' in text

    def test_custom_dropdown_markup_present_for_each_select(self, seeded_app):
        resp = seeded_app["client"].get("/admin/mcp-sources/does-not-exist", headers=_auth(seeded_app))
        assert resp.status_code == 200
        text = resp.text
        for dd_id, target, values in (
            ("edit-transport-dd", "edit-transport", ("stdio", "http", "sse")),
            ("edit-scope-dd", "edit-scope", ("shared", "per_user")),
            ("edit-auth-method-dd", "edit-auth-method", ("", "bearer", "header")),
        ):
            assert f'data-ds-dropdown-target="{target}"' in text
            assert f'id="{dd_id}-btn"' in text
            assert f'id="{dd_id}-menu"' in text
            for value in values:
                assert f'data-value="{value}"' in text

    def test_dropdown_js_module_and_css_are_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/admin/mcp-sources/does-not-exist", headers=_auth(seeded_app))
        assert resp.status_code == 200
        text = resp.text
        assert "js/components/ds_dropdown.js" in text
        assert "css/ds_dropdown.css" in text

    def test_sync_dropdown_helper_wired_into_edit_toggle(self, seeded_app):
        """`edit-toggle-btn` populates the form from server data via direct
        `.value =` assignment on the three selects — a path that bypasses
        ds_dropdown.js's own selectItem() label update. syncDropdown() must
        be called for each so the custom dropdown (paper theme) doesn't show
        a stale label after Edit is opened."""
        resp = seeded_app["client"].get("/admin/mcp-sources/does-not-exist", headers=_auth(seeded_app))
        assert resp.status_code == 200
        text = resp.text
        assert 'syncDropdown("edit-transport", source.transport || "stdio");' in text
        assert 'syncDropdown("edit-auth-method", source.auth_method || "");' in text
        assert 'syncDropdown("edit-scope", source.scope || "shared");' in text
