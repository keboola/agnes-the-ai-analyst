"""Custom design-system dropdown on /store/new's "Category" control (#1055).

`#category` stays a real `<select>` in the DOM (the JS in store_upload.html
reads `.value` at submit time — untouched) with a `ds.dropdown()` custom
button+menu alongside it. Visibility between the two is a CSS theme decision,
not a template one — see `app/web/static/css/paper-skin.css`.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestStoreUploadCategoryDropdown:
    def test_native_select_still_renders_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/store/new", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="category" class="ds-dropdown-native">' in text
        assert '<option value="">— None —</option>' in text

    def test_custom_dropdown_markup_present(self, seeded_app):
        resp = seeded_app["client"].get("/store/new", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'class="ds-dropdown"' in text
        assert 'data-ds-dropdown-target="category"' in text
        assert 'id="category-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="category-dd-menu"' in text
        assert 'id="category-dd-menu"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        # Store categories (Code & Engineering, Data & Analytics, etc.) all
        # carry through into the dropdown options.
        assert 'data-value="Code &amp; Engineering"' in text or "Code &amp; Engineering" in text
        # Default selection (matches the native select's initial default —
        # no option carries `selected`, so the browser default is the first,
        # "— None —" / value="").
        assert 'data-value=""' in text
        assert 'aria-checked="true"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/store/new", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text
