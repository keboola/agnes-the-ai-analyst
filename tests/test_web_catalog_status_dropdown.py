"""Custom design-system dropdown on /catalog's Create/Edit Recipe "Status"
controls (#1055).

`#rcp-status` (Create Recipe modal) and `#ercp-status` (Edit Recipe modal)
stay real `<select>`s (existing status-read wiring in catalog.html is
untouched) with a `ds.dropdown()` custom button+menu alongside each.
Visibility between the two is a CSS theme decision, not a template one —
see `app/web/static/css/paper-skin.css`.

The two admin-group RBAC-requirement `<select>`s in the same modals
(`.rcp-rbac-req` / `.ercp-rbac-req`) are generated dynamically in JS, one
per group fetched from `/api/admin/groups` — not fixed in the Jinja
template — so they are intentionally left out of this conversion.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestCatalogRecipeStatusDropdowns:
    def test_native_selects_still_render_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="rcp-status" class="ds-dropdown-native"' in text
        assert '<option value="prod" selected>Prod</option>' in text
        assert '<select id="ercp-status" class="ds-dropdown-native"' in text
        assert '<option value="prod">Prod</option>' in text

    def test_custom_dropdown_markup_present_for_create_recipe(self, seeded_app):
        resp = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'data-ds-dropdown-target="rcp-status"' in text
        assert 'id="rcp-status-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="rcp-status-dd-menu"' in text
        assert 'id="rcp-status-dd-menu"' in text
        for value in ("prod", "poc", "coming-soon", "draft"):
            assert f'data-value="{value}"' in text

    def test_custom_dropdown_markup_present_for_edit_recipe(self, seeded_app):
        resp = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'data-ds-dropdown-target="ercp-status"' in text
        assert 'id="ercp-status-dd-btn"' in text
        assert 'aria-controls="ercp-status-dd-menu"' in text
        assert 'id="ercp-status-dd-menu"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text

    def test_dynamic_rbac_selects_stay_native_only(self, seeded_app):
        """The per-group RBAC-requirement selects are JS-generated (one per
        admin group), so there is no matching ds.dropdown() pairing for
        them — only the JS class hooks (`.rcp-rbac-req` / `.ercp-rbac-req`)
        should appear, and no paired wrapper referencing them as a target."""
        resp = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert "rcp-rbac-req" in text
        assert "ercp-rbac-req" in text
        assert 'data-ds-dropdown-target="rcp-rbac-req"' not in text
        assert 'data-ds-dropdown-target="ercp-rbac-req"' not in text
