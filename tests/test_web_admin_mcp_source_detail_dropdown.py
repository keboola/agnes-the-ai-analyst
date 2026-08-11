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
            ("edit-auth-method-dd", "edit-auth-method", ("", "bearer", "oauth")),
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
        # Intent, not the literal argument: the edit form must push the loaded
        # value into the paired dropdown. The auth select now resolves its value
        # through `storedAuth` (a stored method this build no longer offers is
        # surfaced as a disabled option instead of silently becoming "none"), so
        # pinning the old expression made the guard fail on a change it does not
        # care about. (Devin Review on #1249.)
        assert 'syncDropdown("edit-auth-method"' in text
        assert 'syncDropdown("edit-scope", source.scope || "shared");' in text


def test_a_removed_auth_method_is_surfaced_not_silently_rewritten(seeded_app):
    """Devin Review on #1249: removing an option rewrote stored data.

    A `<select>` given a value with no matching `<option>` selects the FIRST
    one — "none" here — so opening and saving a server stored under the
    removed "custom header" method silently changed its authentication to
    none, on an edit to some unrelated field.
    """
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "web"
        / "templates"
        / "admin_mcp_source_detail.html"
    ).read_text(encoding="utf-8")

    assert "const knownAuth = Array.from(authSel.options)" in src, "the stored value is never checked"
    assert "opt.disabled = true" in src, "the stale value must not be selectable as-is"
    assert "no longer supported" in src
    assert "edit-auth-method-stale" in src, "nothing tells the admin why"
    # And the notice must not render as an empty strip when there is nothing
    # to say — the same `[hidden]` vs `display` trap fixed on the sibling page.
    assert ".auth-stale[hidden]" in src
    assert src.index(".auth-stale[hidden]") < src.index(".auth-stale {")
