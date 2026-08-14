"""The package detail page (`GET /admin/data-packages/{id}`).

Until this page existed the package — the unit an analyst actually receives —
was the one object in the Data section with no home. Its composition was
edited inside the package-grouped layout on /admin/tables, its sharing on
/admin/data-packages, and the card's own drilldown left the admin area
entirely for the analyst-facing /catalog/p/<slug>.

What this suite pins:

  * the page exists, is admin-gated, and 404s on an unknown id;
  * it states the package's whole life in reading order — what is IN it, who
    can USE it, who actually HAS it;
  * the delivery read-out distinguishes a grant (a permission) from a pull
    (the data actually landing), which is the fact the product could not
    state anywhere before;
  * `query_mode` is worded in plain language while KEEPING the system word,
    so the CLI vocabulary stays learnable;
  * the Packages lens drills HERE, not into /catalog;
  * no tab strip — a lens switch offered one level below the lenses is a way
    to leave the package you opened without noticing;
  * the way back is the page's FIRST line, above the heading;
  * the two states that strand analysts (no tables, shared with nobody) are
    bands ABOVE the work, each carrying the control that fixes it;
  * both flows are the shared right-side drawer, not page-local modals, and
    the Add-tables drawer carries the product's own toolbar (search, facet
    filters, sort) over rows that carry what it filters on.
"""

from __future__ import annotations

import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mk_pkg(name: str = "Revenue Core") -> str:
    from src.repositories import data_packages_repo

    return data_packages_repo().create(
        slug=f"pkg-{uuid.uuid4().hex[:8]}",
        name=name,
        description="The canonical revenue tables.",
        icon=None,
        color=None,
        created_by="test",
    )


class TestPageExists:
    def test_renders_for_an_admin(self, seeded_app):
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        r = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200
        assert "Revenue Core" in r.text

    def test_unknown_package_is_a_404(self, seeded_app):
        c = seeded_app["client"]
        r = c.get(
            "/admin/data-packages/pkg_does_not_exist",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 404

    def test_requires_admin(self, seeded_app):
        """Same gate as every other /admin page — an unauthenticated caller
        must not read the instance's distribution map."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        r = c.get(f"/admin/data-packages/{pkg_id}", follow_redirects=False)
        assert r.status_code in (302, 303, 307, 401, 403)


class TestTheWholeLifeInReadingOrder:
    def test_carries_the_three_panels(self, seeded_app):
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        # what is in it → who can use it → who actually has it
        assert "Tables" in html
        assert "Sharing" in html
        assert 'id="apd-delivery"' in html
        assert "At a glance" in html

    def test_composition_and_sharing_write_the_existing_apis(self, seeded_app):
        """No new endpoints: tables go through the data-packages junction,
        sharing through the SAME `/api/admin/grants` rows a group's Access
        tab writes — so the two ends of one relationship cannot disagree."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "/api/admin/grants" in html
        assert "/api/admin/data-packages/" in html

    def test_no_tab_strip(self, seeded_app):
        """A detail page is one level BELOW the lenses. Offering the lens
        strip here invites a lateral move that silently abandons the package
        you opened; `.apg-back` is the way out and it names where it goes."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "tab-flow__item" not in html
        assert 'class="apg-back"' in html

    def test_the_way_back_is_above_the_heading(self, seeded_app):
        """It used to render inside `{% block page %}`, which sits BELOW the
        hero — so the link out of the page appeared under the title it is
        meant to precede. `{% block page_prehead %}` is the slot for it."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert html.index('class="apg-back"') < html.index('class="page-header__title"')


class TestTheAlarmsComeBeforeTheWork:
    """A package with no tables and a package nobody can see are the two
    states that strand analysts. Both were previously legible only by counting
    rows in a panel; each is now a band above the columns carrying the control
    that fixes it."""

    def test_an_empty_package_says_so_above_the_columns(self, seeded_app):
        c = seeded_app["client"]
        pkg_id = _mk_pkg("Empty One")
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "This package carries no tables" in html
        assert html.index("carries no tables") < html.index('class="apd-cols"')

    def test_an_unshared_package_says_so_above_the_columns(self, seeded_app):
        c = seeded_app["client"]
        pkg_id = _mk_pkg("Unshared One")
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        band = html[html.index('id="apd-unshared-note"') : html.index('class="apd-cols"')]
        assert "hidden" not in band.split(">")[0], "the band must render open when nothing is shared"
        assert "Shared with nobody" in band

    def test_the_unshared_band_is_hidden_once_a_grant_exists(self, seeded_app):
        from src.repositories import resource_grants_repo, user_groups_repo

        c = seeded_app["client"]
        pkg_id = _mk_pkg("Shared One")
        everyone = next(g for g in user_groups_repo().list_all() if g["name"] == "Everyone")
        gid = resource_grants_repo().create(
            group_id=everyone["id"],
            resource_type="data_package",
            resource_id=pkg_id,
            requirement="available",
            assigned_by="test",
        )
        try:
            html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
            band_open = html[html.index('id="apd-unshared-note"') :].split(">")[0]
            assert "hidden" in band_open
        finally:
            resource_grants_repo().delete(gid)


class TestDrawersNotModals:
    """`.ds-drawer` (css/drawer.css) is the product's answer wherever a flow
    is more than one decision — the same chrome the Add-data wizard, the group
    editor and the invite flow ride. Add-tables is search → filter → sort →
    pick several → confirm; a centred card sized for one decision either
    outgrows the viewport or scrolls its own header away."""

    def test_both_flows_are_drawers(self, seeded_app):
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "css/drawer.css" in html
        assert 'id="apd-add-drawer"' in html and 'id="apd-share-drawer"' in html
        assert "ds-drawer__panel" in html
        # …and the page-local modal overlay they replaced is gone.
        assert "apd-overlay" not in html

    def test_the_add_drawer_carries_the_shared_toolbar(self, seeded_app):
        """Search, facet filters and sort — the same `.fbar` + filter_toolbar.js
        the Library runs on, not a second filter implementation. A substring
        match alone is not a way to work three hundred registered tables."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "js/filter_toolbar.js" in html
        assert 'id="apd-add-search"' in html
        assert 'id="apd-add-filter-menu"' in html
        assert 'id="apd-add-sort"' in html

    def test_candidate_rows_carry_what_the_toolbar_filters_on(self, seeded_app):
        """The engine reads facets off each row's data-* attributes, so a row
        missing them is a row no filter can reach."""
        from src.repositories import table_registry_repo

        repo = table_registry_repo()
        tid = f"cand-{uuid.uuid4().hex[:6]}"
        repo.register(
            id=tid,
            name=f"cand_table_{tid[-6:]}",
            source_type="keboola",
            bucket="in.c-cand",
            source_table="cand",
            query_mode="local",
        )
        try:
            c = seeded_app["client"]
            pkg_id = _mk_pkg()
            html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
            row_start = html.index(f'data-pick-table="{tid}"')
            row = html[row_start : row_start + 600]
            for attr in ("data-search", "data-name", "data-source", "data-mode", "data-bucket", "data-synced"):
                assert attr in row, f"candidate row is missing {attr}"
        finally:
            repo.unregister(tid)

    def test_multi_selection_is_the_add_and_the_remove(self, seeded_app):
        """Bundling twelve tables is one action, not twelve — and so is
        un-bundling them. Both ends of the composition editor select many.

        The package is stocked first: the select-all lives in the table's own
        header, so a package with nothing in it has no rows to offer it for."""
        from src.repositories import data_packages_repo, table_registry_repo

        repo = table_registry_repo()
        tid = f"sel-{uuid.uuid4().hex[:6]}"
        repo.register(
            id=tid,
            name=f"sel_table_{tid[-6:]}",
            source_type="keboola",
            bucket="in.c-sel",
            source_table="sel",
            query_mode="local",
        )
        pkg_id = _mk_pkg()
        try:
            c = seeded_app["client"]
            data_packages_repo().add_table(pkg_id, tid, added_by="test")
            html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
            assert 'id="apd-add-save"' in html
            assert 'id="apd-selall"' in html and 'id="apd-selremove"' in html
            assert f'data-pick-row="{tid}"' in html
        finally:
            # Membership first: `table_registry` is the FK parent of the
            # package junction, so unregistering a table still in a package
            # raises rather than cascading.
            data_packages_repo().remove_table(pkg_id, tid)
            repo.unregister(tid)


class TestRemovalAsksFirst:
    """Both removals on this page are destructive and both now confirm.

    The bulk path always did; the per-row ✕ did not — the wrong way round for
    a control that sits on every row, inches from the tier buttons beside it.
    """

    def _html(self, seeded_app) -> str:
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        return c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text

    def test_every_removal_path_confirms(self, seeded_app):
        html = self._html(seeded_app)
        # Three call sites: one table, many tables, and un-sharing a group.
        assert html.count("confirmModal") >= 3
        # Each names its OWN action rather than a generic "Confirm", so the
        # button says what pressing it does.
        assert "Remove table" in html
        assert "Stop sharing" in html

    def test_the_dialogs_name_the_consequence(self, seeded_app):
        """"Are you sure?" cannot tell you the thing that makes this safe to
        confirm — that the table stays registered, or that another group may
        still carry the package."""
        html = self._html(seeded_app)
        # Substrings chosen to sit inside a single source string literal —
        # these messages are built by concatenation, so a phrase that spans a
        # `+` boundary is absent from the HTML even though the reader sees it.
        assert "stays registered" in html
        assert "unless another group they belong to" in html

    def test_the_remove_control_appears_on_the_row_under_the_cursor(self, seeded_app):
        """A ✕ on every row at rest reads as a column of destructive buttons.
        Three details keep hiding it safe, and all three are pinned here
        because dropping any one of them silently breaks a real input:

          * `pointer-events` — an invisible button that still takes clicks is
            a trap, and this one deletes something;
          * `hover: hover` — a touch device has no hover, so hiding it there
            would hide it permanently;
          * `focus-within` / `:focus-visible` — the keyboard path.
        """
        html = self._html(seeded_app)
        block = html.split(".apd-rm {", 1)[1].split("</style>", 1)[0]
        assert "@media (hover: hover)" in block
        assert "pointer-events: none" in block
        assert "focus-within" in block
        assert ":focus-visible" in block
        # Opacity, not display/visibility: the cell keeps its width so no row
        # twitches sideways as the pointer crosses it.
        assert "opacity: 0" in block
        assert "visibility: hidden" not in block


class TestDeliveryReadOut:
    def test_says_nothing_is_delivered_when_shared_with_nobody(self, seeded_app):
        """A package nobody can see is the state that strands analysts, and
        it was previously invisible on every surface."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg("Orphan")
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "share it with a group first" in html

    def test_separates_a_grant_from_a_pull(self, seeded_app):
        """Shared != delivered. A grant is a permission; the data lands only
        when `agnes pull` runs, and `users.last_pull_at` is what turns
        "shared with 14" into "11 actually have it"."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "on their machine" in html
        assert "not yet delivered" in html

    def test_counts_only_automatic_grants_as_reached(self, seeded_app):
        """An OPTIONAL grant only makes the package offerable — counting it
        as reached would overstate delivery, which is the exact confusion
        this panel exists to end."""
        from src.repositories import resource_grants_repo, user_groups_repo

        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        everyone = next(g for g in user_groups_repo().list_all() if g["name"] == "Everyone")
        resource_grants_repo().create(
            group_id=everyone["id"],
            resource_type="data_package",
            resource_id=pkg_id,
            requirement="available",
            assigned_by="test",
        )
        html = c.get(f"/admin/data-packages/{pkg_id}", headers=_auth(seeded_app["admin_token"])).text
        assert "every grant here is optional" in html


class TestPlainLanguageModes:
    def test_leads_with_meaning_and_keeps_the_system_word(self, seeded_app):
        """`query_mode` is vocabulary the admin meets again in `agnes
        catalog` and in the API, so it must survive — but "local" alone
        never said what it does. Both, in that order."""
        from app.web.router import _mode_words

        assert _mode_words("local") == {"label": "Synced copy", "word": "local"}
        assert _mode_words("remote") == {"label": "Live query", "word": "remote"}
        assert _mode_words("materialized") == {"label": "Saved query", "word": "materialized"}
        # A blank mode reads as local — the schema default, and what every
        # other consumer already assumes.
        assert _mode_words("")["word"] == "local"
        assert _mode_words(None)["word"] == "local"

    def test_an_unknown_mode_reads_as_itself(self, seeded_app):
        """A mode nobody has worded yet must never be guessed into the wrong
        one — it passes through verbatim until someone names it."""
        from app.web.router import _mode_words

        assert _mode_words("brand_new") == {"label": "brand_new", "word": "brand_new"}


class TestPackagesLensDrillsHere:
    def test_card_drilldown_is_the_admin_page_not_the_catalog(self, seeded_app):
        """The card is the ADMIN's index of packages; its drilldown used to
        leave the admin area for a read-only page written for a different
        reader."""
        c = seeded_app["client"]
        pkg_id = _mk_pkg()
        html = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"])).text
        assert f"/admin/data-packages/{pkg_id}" in html
