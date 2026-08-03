"""Custom design-system dropdown on /me/profile's "Expires in" token-TTL
control (#1055).

`#create-ttl` stays a real `<select>` in the DOM (existing `ttlIn` read in
_profile_tokens.html's create-token flow is untouched) with a `ds.dropdown()`
custom button+menu alongside it. Visibility between the two is a CSS theme
decision, not a template one — see `app/web/static/css/paper-skin.css`.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestProfileTokenTtlDropdown:
    def test_native_select_still_renders_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get("/me/profile", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="create-ttl" name="expires_in_days"' in text
        assert '<option value="90" selected>90 days</option>' in text

    def test_custom_dropdown_markup_present(self, seeded_app):
        resp = seeded_app["client"].get("/me/profile", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        text = resp.text
        assert 'class="ds-dropdown"' in text
        assert 'data-ds-dropdown-target="create-ttl"' in text
        assert 'id="create-ttl-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="create-ttl-dd-menu"' in text
        assert 'id="create-ttl-dd-menu"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        for value in ("30", "90", "365", ""):
            assert f'data-value="{value}"' in text
        # Default selection (matches the native select's initial `selected`).
        assert 'aria-checked="true"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get("/me/profile", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text
