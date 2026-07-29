"""/library toolbar + grant-aware ownership facets.

The Library page (the renamed, widened former /artefacts) carries a toolbar
(search · ownership segments · Type/Source facets · sort · table⇄grid view) and
is not owner-scoped: it shows what you OWN plus anything shared into a group you
belong to, each row tagged with an ownership facet the toolbar slices on. These
tests lock the toolbar markup and the mine / shared_with_me / shared_by_me
classification.
"""

from __future__ import annotations

import io


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(seeded_app, name: str, token: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _upload(seeded_app, cid: str, filename: str, content: bytes, ctype: str, token: str):
    return seeded_app["client"].post(
        f"/api/collections/{cid}/files",
        files={"files": (filename, io.BytesIO(content), ctype)},
        headers=_auth(token),
    )


def _share_collection_with_user(collection_id: str, user_id: str, group_name: str = "library-share-grp") -> None:
    """Add ``user_id`` to a group and grant that group the collection — the
    minimal path to a "shared with me" artefact."""
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "collection", collection_id):
        grants.create(group_id=grp["id"], resource_type="collection", resource_id=collection_id, assigned_by="test")


def test_library_toolbar_controls_render(seeded_app):
    """Search, the category-based Filter menu, sort and the view toggle render,
    plus the row attributes the client-side engine reads. The four ownership
    tabs and the Type facet are gone — items group by type instead."""
    _create(seeded_app, "Toolbar Demo", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text

    assert 'id="lib-search"' in text
    # Retired: ownership tabs + the Type facet (grouping replaced both).
    assert 'id="lib-own"' not in text
    assert 'data-own="all"' not in text
    assert 'data-facet="type"' not in text
    # Filter menu is now a list of facet CATEGORIES, each with a popover.
    assert 'id="lib-filter-btn"' in text
    assert 'id="lib-filter-menu"' in text
    assert 'class="fbar-cat"' in text
    assert "fbar-cat__pop" in text
    assert 'id="lib-chips"' in text
    assert 'id="lib-sort"' in text
    # Items are grouped into collapsible per-type sections.
    assert "data-lib-sec=" in text
    assert "data-sec-toggle" in text
    assert "data-sec-count" in text
    # Table ⇄ grid view toggle (shared .fbar-view control, table default).
    assert 'data-view="table"' in text
    assert 'data-view="grid"' in text
    # The reusable engine is loaded.
    assert "js/filter_toolbar.js" in text
    # Row attributes the facets read.
    assert 'data-ownership="mine"' in text
    assert 'data-origin="uploaded"' in text
    assert "data-requirement=" in text
    assert "data-search=" in text


def test_source_facet_offers_uploaded_option(seeded_app):
    """The Source facet exposes the artefact's provenance (origin column).
    A freshly uploaded artefact is 'uploaded' and appears as a Source option."""
    _create(seeded_app, "Prov Demo", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert 'data-facet="origin"' in text
    assert 'value="uploaded"' in text


def test_shared_collection_is_shared_with_me_for_grantee(seeded_app):
    """A collection admin owns and shares into the analyst's group shows on the
    analyst's Library as shared_with_me, attributed to the owner."""
    col = _create(seeded_app, "Board Deck", seeded_app["admin_token"])
    _share_collection_with_user(col["id"], "analyst1")

    # Analyst sees it, tagged shared_with_me, owned by "Admin".
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Board Deck" in text
    assert 'data-ownership="shared_with_me"' in text
    assert "Admin" in text  # owner label
    assert "lib-vis--shared" in text

    # The owner sees the SAME collection as shared_by_me (it now carries a grant).
    owner_text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert 'data-ownership="shared_by_me"' in owner_text


def test_unshared_collection_stays_private_to_owner(seeded_app):
    """No grant → the analyst never sees the admin's private collection, and the
    admin's own row is plain 'mine' (not shared_by_me)."""
    _create(seeded_app, "Private Notes", seeded_app["admin_token"])

    analyst_text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Private Notes" not in analyst_text

    admin_text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "Private Notes" in admin_text
    assert 'data-ownership="mine"' in admin_text


def test_files_of_every_format_share_one_files_section(seeded_app):
    """Images, documents and every other format now live in ONE top-level
    "Files" section (the per-format sections were merged), while the row's own
    Type label still names the real format so nothing is lost."""
    col = _create(seeded_app, "Diagram", seeded_app["admin_token"])
    r = _upload(
        seeded_app, col["id"], "diagram.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40, "image/png", seeded_app["admin_token"]
    )
    assert r.status_code in (200, 201), r.text

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    # One merged section, not per-format ones.
    assert 'data-lib-sec="files"' in text
    assert ">Files<" in text
    for retired in ("image", "document", "collection", "spreadsheet"):
        assert f'data-lib-sec="{retired}"' not in text
    # The row still says what the file actually is.
    assert ">Image<" in text


# ---------------------------------------------------------------------------
# Stack: a single "In stack only" toggle (no Available/In Stack submenu)
# ---------------------------------------------------------------------------


def _add_to_stack(seeded_app, collection_id: str, token: str):
    return seeded_app["client"].post(f"/api/stack/artefacts/{collection_id}", headers=_auth(token))


def test_stack_filter_is_a_single_in_stack_only_toggle(seeded_app):
    """The Library's Filter menu leads with an "In stack only" toggle so the
    caller can ask "what can my agent actually use?" in one click — the axis the
    table also acts on via "+ Add to Stack". The two-option Available/In Stack
    category is retired: the states are complementary, so a submenu offering
    both was a longer way to say "everything"."""
    tok = seeded_app["admin_token"]
    added = _create(seeded_app, "Stack Added", tok)
    _create(seeded_app, "Stack Not Added", tok)
    assert _add_to_stack(seeded_app, added["id"], tok).status_code == 200

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    # One checkbox, directly in the menu — not a category with a popover.
    assert "fbar-menu__toggle" in text
    assert 'data-facet="stack" value="in_stack"' in text
    assert ">In stack only<" in text
    assert 'data-cat="stack"' not in text
    assert 'data-facet="stack" value="available"' not in text
    # Row attribute the toggle slices on, in both states.
    assert 'data-stack="in_stack"' in text
    assert 'data-stack="available"' in text


def test_stack_toggle_shows_the_matching_item_count(seeded_app):
    """The toggle carries the tally of what it would leave standing, so the
    caller knows the size of their Stack before flipping it."""
    tok = seeded_app["admin_token"]
    import re

    for name in ("Counted A", "Counted B", "Uncounted"):
        col = _create(seeded_app, name, tok)
        if name.startswith("Counted"):
            assert _add_to_stack(seeded_app, col["id"], tok).status_code == 200

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    badge = re.search(r'<span class="fbar-menu__opt-n" data-stack-count>(\d+)</span>', text)
    assert badge, "the In-stack-only toggle rendered no count"
    rendered = int(badge.group(1))
    # Every row carrying the in-Stack state is counted — the two just added plus
    # whatever auto-membership already put there (grants, installs).
    in_stack_rows = len(re.findall(r'<tr\b[^>]*data-stack="in_stack"[^>]*>', text))
    top_level = len(
        [r for r in re.findall(r'<tr\b[^>]*data-stack="in_stack"[^>]*>', text) if "data-parent-id" not in r]
    )
    assert rendered == top_level, (
        f"count {rendered} != {top_level} top-level in-Stack rows ({in_stack_rows} incl. children)"
    )
    assert rendered >= 2


def test_stack_toggle_keeps_locked_admin_required_items(seeded_app):
    """A locked admin-required membership IS a membership, so the row carries the
    in-Stack state "In stack only" slices on — the tier is filterable on its own
    Optional/Required category, not by hiding the row here."""
    import uuid

    from src.db import get_system_db
    from src.repositories.data_packages import DataPackagesRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    pkg_id = DataPackagesRepository(conn).create(
        name="Mandated Toggle Package", slug="req-toggle-pkg", description="d", icon=None, color=None, created_by="t"
    )
    gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) VALUES (?, ?, 'data_package', ?, 'required', CURRENT_TIMESTAMP, 't')",
        [str(uuid.uuid4()), gid, pkg_id],
    )
    conn.close()

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    start = text.rindex("<tr", 0, text.index("Mandated Toggle Package"))
    row = text[start : text.index("</tr>", start)]
    assert "lib-instack--required" in row  # locked membership
    assert 'data-stack="in_stack"' in row  # …and the toggle keeps it


def test_stack_toggle_absent_when_it_would_change_nothing(seeded_app):
    """No dead filters: with nothing in the Stack the toggle would only ever
    empty the page, so it doesn't render at all."""
    tok = seeded_app["analyst_token"]
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    if 'data-stack="in_stack"' not in text:
        assert "fbar-menu__toggle" not in text


def test_stack_state_is_visible_on_every_row(seeded_app):
    """A filter must never hide a row for an invisible reason: an in-Stack row
    carries the badge, and an available-but-addable one the Add button."""
    tok = seeded_app["admin_token"]
    added = _create(seeded_app, "Badged", tok)
    _create(seeded_app, "Addable", tok)
    _add_to_stack(seeded_app, added["id"], tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    # Both states share the `.lib-stackpill` box (so the in-place swap can't
    # shift the row) and then diverge: `.lib-instack` is the status, the Add
    # button is the action.
    assert 'class="lib-stackpill lib-instack"' in text
    assert "data-add-to-stack=" in text


def test_granted_resources_report_in_stack_not_addable(seeded_app):
    """Auto-membership: a grant on the caller's group puts a resource in their
    Stack with no action, so those rows say "In Stack" and offer no Add."""
    import re

    from src.db import get_system_db
    from src.repositories import data_packages_repo
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("stack-facet-grp") or groups.create(
        name="stack-facet-grp", description="t", created_by="t"
    )
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    pkg = data_packages_repo().create(
        name="Auto Stacked", slug="auto-stacked", description="d", icon=None, color=None, created_by="admin"
    )
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="data_package", resource_id=pkg, assigned_by="admin"
    )

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = re.search(r'<tr[^>]*data-type="data_package"[^>]*>', text)
    assert row, "data_package row not found"
    assert 'data-stack="in_stack"' in row.group(0)


def test_every_row_carries_a_stack_state(seeded_app):
    """A stateless row would be silently dropped by "In stack only" while its
    own pill claims membership — the filter must never hide a row for an
    invisible reason. An authored skill counts as Available: reachable, but not
    in the Stack."""
    import re

    tok = seeded_app["admin_token"]
    _create(seeded_app, "State Check", tok)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    rows = re.findall(r'<tr\b[^>]*data-item-id="[^"]+"[^>]*>', text)
    assert rows, "no library rows rendered"
    stateless = [r for r in rows if 'data-stack=""' in r]
    assert not stateless, f"{len(stateless)} row(s) have no Stack state"


# ---------------------------------------------------------------------------
# Sorting lives on the column headers, not in the toolbar
# ---------------------------------------------------------------------------


def test_sortable_columns_are_name_owner_and_sharing(seeded_app):
    """A column header is where a reader asks "order by this", so sorting moved
    out of the toolbar and onto the columns. Type is excluded by design: the
    list is already GROUPED by type into these very sections, so ordering by it
    inside one of them is a no-op. Actions is not data."""
    import re

    _create(seeded_app, "Sort Columns", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text

    head = text.split("<thead>", 1)[1].split("</thead>", 1)[0]
    keys = re.findall(r'data-sort-key="([^"]+)"', head)
    assert keys == ["name", "owner", "sharing"], f"sortable columns drifted: {keys}"
    for dead in ("Type", "Actions"):
        before, _, _ = head.partition(f">{dead}<")
        assert _, f"{dead} header missing"
        assert "lib-sort" not in before.rsplit("<th", 1)[-1], f"{dead} must not be sortable"


def test_every_sortable_column_opens_a_to_z(seeded_app):
    """All three sort text the reader can see, so all three open ascending —
    there is no date column to want newest-first."""
    import re

    _create(seeded_app, "Sort Direction", seeded_app["admin_token"])
    head = (
        seeded_app["client"]
        .get("/library", headers=_auth(seeded_app["admin_token"]))
        .text.split("<thead>", 1)[1]
        .split("</thead>", 1)[0]
    )
    pairs = re.findall(r'data-sort-key="([^"]+)" data-sort-first="([^"]+)"', head)
    assert dict(pairs) == {"name": "asc", "owner": "asc", "sharing": "asc"}


def test_sortable_header_keeps_the_plain_column_header_look(seeded_app):
    """A sortable column is a column first and a control second: same muted
    uppercase as the headers that don't sort, with a chevron as the entire
    difference. The <button> UA style resets `text-transform`, so the header
    would otherwise read "Name" in a row of "TYPE" / "OWNER"."""
    # One item, or the type sections — and with them the <thead> — don't render.
    _create(seeded_app, "Header Look", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "text-transform: inherit; letter-spacing: inherit;" in text
    assert 'Name <span class="lib-sort__dir"' in text
    # No accent recolour on the active column — the chevron carries the state.
    assert ".lib-sort.is-sorted { color: var(--ds-text-primary); }" in text


def test_sharing_sorts_on_the_label_not_the_internal_key(seeded_app):
    """`data-visibility` ("private" / "shared" / "workspace") does not sort into
    the order the column is READ in, so the header sorts on the label the cell
    actually shows."""
    import re

    _create(seeded_app, "Shared Sort", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "sharing: 'data-sharing'" in text
    labels = re.findall(r'data-sharing="([^"]*)"', text)
    assert labels, "no row carries the Sharing sort key"
    assert any(v and not v.islower() for v in labels), f"looks like keys, not labels: {set(labels)}"


def test_toolbar_sort_select_is_grid_only(seeded_app):
    """In table view the headers ARE the sort control, so the toolbar select
    would be a second live readout of one order — it ships hidden and the engine
    reveals it only in grid view, where there are no headers to click. Hidden by
    the engine rather than by Jinja because the view is a client-side, persisted
    choice the server cannot know."""
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert '<div class="fbar-select" id="lib-sortwrap" hidden>' in text
    assert "headers: '.lib-table .lib-sort', wrap: '#lib-sortwrap'" in text

    js = seeded_app["client"].get("/static/js/filter_toolbar.js").text
    # One order, two controls, both re-synced from it.
    assert "function setSort(value)" in js
    assert "function syncSortControls()" in js
    assert "if (sortWrapEl && sortBtns.length) sortWrapEl.hidden = !grid;" in js
    # The header drives the accessible sorted state, not colour alone.
    assert "th.setAttribute('aria-sort'" in js
