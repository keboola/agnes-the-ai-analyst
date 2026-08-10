"""Custom design-system dropdown on the Library's "Sort library" control (#1055).

`#lib-sort` stays a real `<select>` in the DOM (existing `sortSelect`/toolbar
engine wiring in library.html is untouched) with a `ds.dropdown()` custom
button+menu alongside it, inside the same `#lib-sortwrap` container so grid/
table-view visibility toggling (driven by `filter_toolbar.js`) still governs
both together. Visibility between select and custom dropdown is a CSS theme
decision, not a template one — see `app/web/static/css/paper-skin.css`.
"""

from __future__ import annotations
import pytest


@pytest.fixture(autouse=True)
def _rail_layout(monkeypatch):
    """The dropdown conversion landed on the rail redesign's library.html;
    topnav renders the frozen library_legacy.html (spec 2026-08-07 wave 2),
    which the "no new features on the legacy path" policy keeps untouched —
    guarded by tests/test_ui_layout_theme.py::TestDefaultContentParity."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestLibrarySortDropdown:
    def test_native_select_still_renders_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert '<div class="fbar-select" id="lib-sortwrap" hidden>' in text
        assert 'id="lib-sort" aria-label="Sort library" class="ds-dropdown-native"' in text
        assert '<option value="added_desc">Recently added</option>' in text

    def test_custom_dropdown_markup_present(self, seeded_app):
        resp = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        # Wrapper is paired to the real select for backward-compatible change
        # wiring — the JS module reads this to sync .value + dispatch change.
        assert 'class="ds-dropdown"' in text
        assert 'data-ds-dropdown-target="lib-sort"' in text
        # Trigger button: accessible button+menu contract.
        assert 'id="lib-sort-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="lib-sort-dd-menu"' in text
        # Menu: role=menu of mutually-exclusive role=menuitemradio choices.
        assert 'id="lib-sort-dd-menu"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        for value in (
            "added_desc",
            "added_asc",
            "name_asc",
            "name_desc",
            "owner_asc",
            "owner_desc",
            "sharing_asc",
            "sharing_desc",
        ):
            assert f'data-value="{value}"' in text
        # Default selection (matches the native select's initial `selected`).
        assert 'aria-checked="true"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text
