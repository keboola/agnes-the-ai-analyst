"""Custom design-system dropdown on the MCP sources list's create modal (#1055).

Each of the create-modal's three `<select>`s — transport, secret scope, auth
method — stays a real `<select>` in the DOM (existing JS wiring:
`syncTransportFields` / `confirm-create-btn` untouched) with a `ds.dropdown()`
custom button+menu alongside it. Visibility between the two is a CSS theme
decision, not a template one — see `app/web/static/css/paper-skin.css`.
"""

from __future__ import annotations


def _auth(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


class TestMcpSourcesCreateModalDropdowns:
    def test_native_selects_still_render_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/admin/mcp-sources", headers=_auth(seeded_app))
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="new-transport" class="ds-dropdown-native">' in text
        assert '<select id="new-scope" class="ds-dropdown-native">' in text
        assert '<select id="new-auth-method" class="ds-dropdown-native">' in text

    def test_custom_dropdown_markup_present_for_each_select(self, seeded_app):
        resp = seeded_app["client"].get("/admin/mcp-sources", headers=_auth(seeded_app))
        assert resp.status_code == 200
        text = resp.text
        for dd_id, target, values in (
            ("new-transport-dd", "new-transport", ("stdio", "http", "sse")),
            ("new-scope-dd", "new-scope", ("shared", "per_user")),
            ("new-auth-method-dd", "new-auth-method", ("", "bearer", "header")),
        ):
            assert f'data-ds-dropdown-target="{target}"' in text
            assert f'id="{dd_id}-btn"' in text
            assert f'id="{dd_id}-menu"' in text
            for value in values:
                assert f'data-value="{value}"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'role="menuitemradio"' in text

    def test_dropdown_js_module_and_css_are_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/admin/mcp-sources", headers=_auth(seeded_app))
        assert resp.status_code == 200
        text = resp.text
        assert "js/components/ds_dropdown.js" in text
        assert "css/ds_dropdown.css" in text

    def test_sync_dropdown_helper_wired_into_form_reset(self, seeded_app):
        """`open-create-btn` resets the form via direct `.value =` assignment
        on the three selects — a path that bypasses ds_dropdown.js's own
        selectItem() label update. syncDropdown() must be called for each so
        the custom dropdown (paper theme) doesn't show a stale label after a
        previous selection + modal re-open."""
        resp = seeded_app["client"].get("/admin/mcp-sources", headers=_auth(seeded_app))
        assert resp.status_code == 200
        text = resp.text
        assert 'syncDropdown("new-transport", "stdio");' in text
        assert 'syncDropdown("new-auth-method", "");' in text
        assert 'syncDropdown("new-scope", "shared");' in text
