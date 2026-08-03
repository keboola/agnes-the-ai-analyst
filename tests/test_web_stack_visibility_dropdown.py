"""Custom design-system dropdown on My Stack's "Add artefacts" picker
"Filter by visibility" control (#1055).

`#stk-af-visibility` stays a real `<select>` in the DOM (existing
`afVisibility`/`afRenderList()` wiring in stack_unified.html is untouched)
with a `ds.dropdown()` custom button+menu alongside it. Visibility between
the two is a CSS theme decision, not a template one — see
`app/web/static/css/paper-skin.css`.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestStackVisibilityFilterDropdown:
    def test_native_select_still_renders_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/stack", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="stk-af-visibility" aria-label="Filter by visibility" class="ds-dropdown-native">' in text
        assert '<option value="">All visibility</option>' in text

    def test_custom_dropdown_markup_present(self, seeded_app):
        resp = seeded_app["client"].get("/stack", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'data-ds-dropdown-target="stk-af-visibility"' in text
        assert 'id="stk-af-visibility-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="stk-af-visibility-dd-menu"' in text
        assert 'id="stk-af-visibility-dd-menu"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        for value in ("", "private", "shared", "workspace"):
            assert f'data-value="{value}"' in text
        # Default selection (matches the native select's initial `selected`).
        assert 'aria-checked="true"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/stack", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text
