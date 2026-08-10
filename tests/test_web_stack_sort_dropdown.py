"""Custom design-system dropdown on My Stack's "Sort your stack" control (#1055).

`#stk-sort` stays a real `<select>` in the DOM (existing `sortSelect`/`rebuild()`
wiring in stack_unified.html is untouched) with a `ds.dropdown()` custom
button+menu alongside it. Visibility between the two is a CSS theme decision,
not a template one — see `app/web/static/css/paper-skin.css`.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestStackSortDropdown:
    def test_native_select_still_renders_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/stack", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="stk-sort" aria-label="Sort your stack"' in text
        assert '<option value="name" selected>Name (A' in text

    def test_custom_dropdown_markup_present(self, seeded_app):
        resp = seeded_app["client"].get("/stack", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        text = resp.text
        # Wrapper is paired to the real select for backward-compatible change
        # wiring — the JS module reads this to sync .value + dispatch change.
        assert 'class="ds-dropdown"' in text
        assert 'data-ds-dropdown-target="stk-sort"' in text
        # Trigger button: accessible button+menu contract (generalized from
        # the chat composer's "+" upload menu).
        assert 'id="stk-sort-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="stk-sort-dd-menu"' in text
        # Menu: role=menu of mutually-exclusive role=menuitemradio choices.
        assert 'id="stk-sort-dd-menu"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        for value in ("name", "recent", "type"):
            assert f'data-value="{value}"' in text
        # Default selection (matches the native select's initial `selected`).
        assert 'aria-checked="true"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/stack", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text
