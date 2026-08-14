"""Creating a Data Package: one shared right-side drawer, opened in place.

Three things had gone wrong with the create form, and all three are the same
mistake — the form was page furniture on /admin/tables rather than a component:

  * it was a centred modal carrying the tallest form in the admin surface
    (name, slug, description, lifecycle, category, icon, colour, cover image
    AND a group-access matrix), so on a laptop its footer sat over its own
    last field. Setup flows in this product come from the right;
  * the Packages lens — where "+ New package" belongs — had no copy of it, so
    its button LINKED to `/admin/tables?new_package=1`: asking for a new
    package on the Data packages tab switched you to the Tables tab;
  * the native `<select>`, colour input and file input inside it were
    undressed browser chrome next to fields that were not.

What this suite pins:

  * the Packages lens opens the drawer in place — a button, not a link out;
  * /admin/tables no longer carries the create MARKUP, only the component and
    the two entry points that name it (`?new_package=1` from the legacy
    catalog CTA, and the chip-input's `chip-create`);
  * both consumers link the shared drawer chrome AND the component, since a
    consumer that links one without the other renders an unstyled form or no
    form at all;
  * the controls the shared field rules do not cover are dressed in the shared
    sheet (`.fbar-select`, `.ds-drawer__color`, `.ds-drawer__file`), not left
    native and not re-invented per page.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES = Path("app/web/templates")
STATIC = Path("app/web/static")

COMPONENT = STATIC / "js" / "components" / "package_drawer.js"
DRAWER_CSS = STATIC / "css" / "drawer.css"
COMPONENT_CSS = STATIC / "css" / "package_drawer.css"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestTheComponentExists:
    def test_the_flow_is_a_component_not_a_page_script(self) -> None:
        assert COMPONENT.exists(), "the create-package flow must live in js/components/package_drawer.js"
        assert COMPONENT_CSS.exists(), "the component's own two objects belong in css/package_drawer.css"
        src = COMPONENT.read_text(encoding="utf-8")
        # The public surface every consumer calls.
        assert "window.AgnesPackageDrawer = { open: open, close: close };" in src
        # The shared drawer chrome, not a card of its own vocabulary.
        assert "root.className = 'ds-drawer';" in src
        assert "ds-drawer__panel" in src and "ds-drawer__foot" in src
        assert "modal-overlay" not in src

    def test_the_create_response_carries_only_an_id_so_the_name_rides_along(self) -> None:
        """`POST /api/admin/data-packages` answers `{id}` and nothing else.

        A callback reading `pkg.name` off that body gets `undefined` — which is
        what the chip and the toast used to show ('Data Package "undefined"
        created'). The component must hand the caller what it SENT plus the id.
        """
        src = COMPONENT.read_text(encoding="utf-8")
        assert "var pkg = { id: created.id, name: name, slug: slug };" in src

    def test_the_undressed_controls_are_dressed_in_the_shared_sheet(self) -> None:
        css = DRAWER_CSS.read_text(encoding="utf-8")
        for selector in (".ds-drawer__color", ".ds-drawer__file", ".ds-drawer__disclose", ".ds-drawer__row"):
            assert selector in css, f"{selector} must live in the shared drawer sheet"
        # `::file-selector-button` is the ONLY part of a file input that can be
        # styled; without it the control is a native grey button beside fields
        # that are not.
        assert "::file-selector-button" in css
        src = COMPONENT.read_text(encoding="utf-8")
        # The select borrows the product's own select chrome rather than a
        # drawer-local copy of it.
        assert "fbar-select" in src
        # …and the access tier borrows the product's segmented control.
        assert "fbar-seg" in src


class TestThePackagesLensOpensItInPlace:
    def test_new_package_is_a_button_not_a_link_to_another_lens(self, seeded_app) -> None:
        c = seeded_app["client"]
        html = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert "data-adp-new-package>+ New package</button>" in " ".join(html.split())
        # The bug this replaced: creating a package moved you to the Tables lens.
        assert "/admin/tables?new_package=1" not in html

    def test_the_empty_state_opens_the_same_drawer(self, seeded_app) -> None:
        """An instance with no packages is exactly where "+ New package" matters
        most, and its CTA was the same cross-lens link."""
        src = (TEMPLATES / "admin_data_packages.html").read_text(encoding="utf-8")
        empty_block = src[src.index("No Data Packages yet") : src.index("Memory Domains")]
        assert "data-adp-new-package" in empty_block
        assert "new_package=1" not in empty_block

    def test_the_lens_links_the_chrome_and_the_component(self, seeded_app) -> None:
        c = seeded_app["client"]
        html = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert "css/drawer.css" in html
        assert "css/package_drawer.css" in html
        assert "js/components/package_drawer.js" in html

    def test_suggest_by_bucket_still_crosses_deliberately(self, seeded_app) -> None:
        """Not every cross-lens link was a slip: that flow reads the table
        registry and assigns rows, so the table list IS its subject."""
        c = seeded_app["client"]
        html = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert "/admin/tables?group_by_bucket=1" in html


class TestTheTablesLensKeepsTheEntryPointsAndDropsTheMarkup:
    def test_the_modal_markup_is_gone(self, seeded_app) -> None:
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        assert 'id="createDataPackageModal"' not in html
        # Every field id of the old form went with it — a leftover would be a
        # second, invisible copy of the same inputs.
        for field in ("cdp-name", "cdp-slug", "cdp-status", "cdp-color", "cdp-rbac-rows"):
            assert f'id="{field}"' not in html, field

    def test_the_component_and_its_two_entry_points_stay(self, seeded_app) -> None:
        c = seeded_app["client"]
        html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
        assert "js/components/package_drawer.js" in html
        assert "css/package_drawer.css" in html
        # `?new_package=1` — the legacy catalog CTA lands here with it.
        assert "new_package" in html
        # The chip-input's "+ Create new" tail row, which must keep the host so
        # the new package comes back as a chip on the table being edited.
        assert "chip-create" in html
        assert "chipHost: host" in html
