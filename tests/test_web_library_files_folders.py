"""Library Files section: collections as folders, per-file pages, drag-to-move.

Every file format (images, documents, anything else) shares ONE top-level
"Files" section, with multi-file collections nested inside it as folders:

  - a single-file artefact IS its file — a loose row, draggable into a folder;
  - a multi-file collection is a folder row: a drop target that expands to its
    files, each of which has its own detail page AND its own sharing;
  - non-file kinds (skills, data packages, memory, …) stay separate top-level
    sections and are never drop targets.

Drag-and-drop is `POST /api/collections/{src}/files/{fid}/move`.
"""

from __future__ import annotations

import io


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _collection(seeded_app, name: str, token: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _upload(
    seeded_app,
    cid: str,
    filename: str,
    token: str,
    content: bytes = b"# doc\n" + b"x" * 300,
    ctype: str = "text/markdown",
):
    return seeded_app["client"].post(
        f"/api/collections/{cid}/files",
        files={"files": (filename, io.BytesIO(content), ctype)},
        headers=_auth(token),
    )


def _files(seeded_app, cid: str, token: str) -> list:
    r = seeded_app["client"].get(f"/api/collections/{cid}/files", headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()["files"]


def _folder(seeded_app, name: str, token: str, names=("a.md", "b.md")) -> dict:
    col = _collection(seeded_app, name, token)
    for n in names:
        assert _upload(seeded_app, col["id"], n, token).status_code in (200, 201)
    return col


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_one_files_section_holds_loose_files_and_folders(seeded_app):
    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Solo Pic", tok)
    _upload(seeded_app, solo["id"], "pic.png", tok, b"\x89PNG\r\n\x1a\n" + b"0" * 40, "image/png")
    _folder(seeded_app, "Board Pack", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert 'data-lib-sec="files"' in text
    # The per-format and standalone Collections sections are retired.
    for retired in ("image", "document", "collection", "spreadsheet", "pdf"):
        assert f'data-lib-sec="{retired}"' not in text
    # Both the loose file and the folder are in it.
    assert "Solo Pic" in text
    assert "Board Pack" in text


def test_groups_are_bands_of_one_united_list(seeded_app):
    """Every type is a group wearing the same `.fbar-group` header component My
    Stack's Required / Added-by-you split uses, and the groups sit inside ONE
    framed list (`.lib-list`) rather than as separately-boxed sections."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "United List Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert text.count('<div class="lib-list">') == 1
    assert re.search(r'<section class="fbar-group lib-group[^"]*" data-lib-sec="', text)
    assert 'class="fbar-groupband lib-band"' in text
    assert 'class="fbar-grouptoggle"' in text
    # …and the retired per-section heading chrome is gone.
    assert "fbar-sec__head" not in text
    assert 'class="fbar-sec"' not in text


def test_each_group_has_its_own_column_header_on_one_shared_grid(seeded_app):
    """Each group carries its OWN <thead> — that is what lets the header be
    sticky per group — while every group repeats the SAME <colgroup>, so the
    columns of every header line up with the columns of every group's rows."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Own Header Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    groups = re.findall(r'<section class="fbar-group lib-group[^"]*" data-lib-sec="([^"]+)"', text)
    assert groups
    # One table, one colgroup and one thead per group — no page-level header that
    # would pin a second row above the group's own.
    assert text.count('<table class="data-table lib-table">') == len(groups)
    assert text.count("<thead>") == len(groups)
    assert text.count('<col class="lib-col-type">') == len(groups)
    # Every colgroup is the same, so the grids cannot drift apart.
    colgroups = re.findall(r"<colgroup>(.*?)</colgroup>", text, re.S)
    assert len(set(cg.split() and " ".join(cg.split()) for cg in colgroups)) == 1
    # Fixed layout is what makes the shared colgroup binding rather than advisory.
    assert "table-layout: fixed" in text


def test_group_headers_are_sticky_and_replace_one_another(seeded_app):
    """The band and its column header pin per group. Both offsets read the same
    `--lib-bandh` token so the column header lands exactly under its band, and
    the sticky boxes are the group's own section and table — a sticky table CELL
    is not constrained by its row group, so one table per group is what keeps the
    headers replacing each other instead of stacking up."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Sticky Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert ".lib-band { position: sticky; top: 0;" in text
    assert "position: sticky; top: var(--lib-bandh);" in text
    assert "--lib-bandh: 40px;" in text
    assert "height: var(--lib-bandh);" in text
    # Sticky dies inside a scroll container: the primitive's `overflow: hidden`
    # on the table is overridden, the one-frame wrapper clips instead of hiding,
    # and horizontal scroll is scoped to the narrow breakpoint.
    assert ".lib-table { overflow: visible; }" in text
    assert "overflow: clip" in text
    assert "@media (max-width: 980px)" in text
    # A folded group must not leave a column header pinned behind it.
    assert ".lib-group.is-collapsed .lib-tablewrap" in text


def test_grid_view_drops_the_frame_and_the_filled_band(seeded_app):
    """The frame and the filled sticky band are TABLE chrome. In grid view the
    cards carry their own border and background, so the list frame is dropped,
    the group header goes transparent and unpadded, and it stands off its cards
    by the grid's own gap. Which view is on is read off the projected grid being
    visible, so the styling needs no JS of its own."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Grid Chrome Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    grid_on = ":has(> .fbar-grid:not([hidden]))"
    # The frame goes.
    assert ".lib-list:has(> .lib-group > .fbar-grid:not([hidden]))" in text
    # The band goes transparent, unpinned, unbordered — and off the cards.
    assert f".lib-group{grid_on} > .lib-band" in text
    assert "position: static; background: none; border-bottom: 0; box-shadow: none;" in text
    assert "margin-bottom: 14px;" in text
    # No hover fill either — that would put the filled bar back.
    assert f".lib-group{grid_on} > .lib-band .fbar-grouptoggle" in text
    assert "padding-left: 0; padding-right: 0; background: none;" in text
    # Folded → header only, no gap kept for cards that aren't shown.
    assert f".lib-group.is-collapsed{grid_on} > .lib-band" in text
    # Separate blocks rather than bands of one list.
    assert f".lib-group{grid_on} + .lib-group" in text
    # …while table view keeps its frame and its sticky band.
    assert ".lib-list { border: 1px solid var(--ds-border);" in text


def test_group_header_carries_label_count_and_hint(seeded_app):
    """The header is the whole component: collapse caret, kind glyph, label, a
    live count, and one line saying what the group holds (the slot My Stack
    fills with "Optional resources you added.")."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Hinted Deck", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    head = re.search(r'<button type="button" class="fbar-grouptoggle".*?</button>', text, re.S).group(0)
    assert 'data-sec-toggle="files"' in head  # keyed, so both hosts fold together
    # Rendered folded — the page's default. The script re-opens whatever the
    # caller has stored; rendering open would flash the unfolded list first.
    assert 'aria-expanded="false"' in head
    assert "fbar-group__caret" in head
    assert "lib-sec-icon" in head  # the per-type glyph
    assert ">Files<" in head
    assert "data-sec-count" in head
    assert "Uploaded by you, or generated by an agent." in head


def test_groups_render_folded_in_a_fixed_order(seeded_app):
    """Every group arrives FOLDED, and they arrive in one canonical order.

    Folded is the whole point of the ordering: the first screen is the list of
    sections itself, so their sequence is what a caller reads on arrival rather
    than an incidental detail below the fold. The order runs outward from what
    an agent is built on — governed data, then the capabilities acting on it,
    then the caller's own files, with curated Memory last.

    Asserted as a SUBSEQUENCE so the test doesn't depend on which kinds the
    fixture happens to seed: whatever renders must appear in this relative
    order, and a new kind added to the middle doesn't break it.
    """
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Ordered Deck", tok)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text

    canonical = ["data_package", "plugin", "skill", "agent", "recipe", "files", "memory_domain"]
    rendered = re.findall(r'<section class="fbar-group lib-group[^"]*" data-lib-sec="([^"]+)"', text)
    assert rendered, "no groups rendered"
    assert rendered == [k for k in canonical if k in rendered]

    # …and every one of them is folded, class and ARIA agreeing.
    sections = re.findall(r'<section class="(fbar-group lib-group[^"]*)" data-lib-sec=', text)
    assert all("is-collapsed" in cls for cls in sections)
    assert text.count('class="fbar-grouptoggle" data-sec-toggle') == len(rendered)
    assert 'aria-expanded="true"' not in text.split('<div class="lib-list">', 1)[1]


def test_folder_row_is_a_drop_target_and_expands(seeded_app):
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Expandable", tok)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert 'data-folder="1"' in text
    assert 'data-drop-target="1"' in text
    assert "data-folder-toggle=" in text
    # Its files are rendered as child rows, hidden until expanded.
    assert "data-parent-id=" in text
    assert "lib-row--child" in text


def test_loose_file_is_draggable_and_folder_is_not(seeded_app):
    """Files drag INTO folders; folders don't nest, so they never drag."""
    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Draggable One", tok)
    _upload(seeded_app, solo["id"], "one.md", tok)
    _folder(seeded_app, "Target Folder", tok)
    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert 'draggable="true"' in text
    assert 'draggable="false"' in text


def test_section_count_reflects_the_hierarchy(seeded_app):
    """The Files badge counts TOP-LEVEL entries — a folder counts once, not
    once per file inside it."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Count Solo", tok)
    _upload(seeded_app, solo["id"], "s.md", tok)
    _folder(seeded_app, "Count Folder", tok, names=("x.md", "y.md", "z.md"))

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    m = re.search(r'data-lib-sec="files".*?data-sec-count>(\d+)<', text, re.S)
    assert m, "files section badge not found"
    # 2 top-level entries (one loose file + one folder), not 4 files.
    assert int(m.group(1)) == 2


def test_collections_are_listed_before_loose_files(seeded_app):
    """Inside Files the collections form their own block at the top — the
    containers first, the loose files after them."""
    import re

    tok = seeded_app["admin_token"]
    # Created loose-file-first, so recency order alone would put it on top.
    solo = _collection(seeded_app, "Order Solo", tok)
    _upload(seeded_app, solo["id"], "solo.md", tok)
    _folder(seeded_app, "Order Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    # Scoped to the group's own <tbody> — every group shares one table now, so
    # reading to `</table>` would sweep in the groups below it.
    body = re.search(r'data-lib-sec="files".*?</tbody>', text, re.S)
    assert body, "files section not found"
    rows = re.findall(r'<tr class="lib-row([^"]*)"', body.group(0))
    tops = [r for r in rows if "lib-row--child" not in r]
    # Every folder row precedes every loose-file row.
    kinds = ["folder" if "lib-row--folder" in r else "file" for r in tops]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "folder" else 1), kinds
    assert "folder" in kinds and "file" in kinds


def test_collection_rows_wear_a_folder_glyph_and_files_do_not(seeded_app):
    """A collection is visually a folder in the Files table; a loose file keeps
    the document glyph, so the two never read as the same kind of thing."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Glyph Solo", tok)
    _upload(seeded_app, solo["id"], "g.md", tok)
    _folder(seeded_app, "Glyph Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    folder_row = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S)
    file_row = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S)
    assert folder_row and file_row
    assert "lib-icon--folder" in folder_row.group(0)
    assert "M4 7.5A1.5 1.5 0 0 1 5.5 6" in folder_row.group(0)  # folder glyph
    assert "lib-icon--folder" not in file_row.group(0)
    assert "M7 4h7l4 4v12H7z" in file_row.group(0)  # single-document glyph


def test_every_row_is_the_link_and_carries_no_open_button(seeded_app):
    """The whole row opens the item — there is no per-row Open button."""
    import re

    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Clickable Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    row = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S)
    assert row
    assert f'data-href="/library/{col["slug"]}"' in row.group(0)
    assert ">Open" not in row.group(0)


def test_every_section_shares_one_fixed_column_grid(seeded_app):
    """Each section owns its own <table>, so without a shared colgroup their
    columns drift apart and Type/Sharing/Actions land at a different x per
    section. Fixed layout + one colgroup is what keeps the page on one grid —
    and what makes a nested file row ride its folder's columns exactly."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Grid Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert "table-layout: fixed" in text
    tables = re.findall(r"<table class=\"data-table lib-table\">(.*?)</table>", text, re.S)
    assert tables, "no library tables rendered"
    groups = [re.search(r"<colgroup>.*?</colgroup>", t, re.S) for t in tables]
    assert all(groups), "a section rendered without the shared column grid"
    # Byte-identical colgroup in every section = identical widths everywhere.
    assert len({re.sub(r"\s+", " ", g.group(0)) for g in groups}) == 1


def test_nested_files_use_the_plain_row_with_a_connector(seeded_app):
    """Nesting is carried by indentation + a connector rail, not a tinted band:
    a child row is the same white row as any other, and the last child is marked
    so its rail can stop at the elbow."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Connector Folder", tok, names=("c1.md", "c2.md"))

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    kids = re.findall(r'<tr class="lib-row lib-row--file lib-row--child[^"]*"', text)
    assert len(kids) >= 2
    assert any("lib-row--lastchild" in k for k in kids), "last child not marked"
    # Exactly one last-child per folder.
    assert sum(1 for k in kids if "lib-row--lastchild" in k) == 1


def test_actions_hold_only_the_reserved_stack_slot(seeded_app):
    """One fixed slot keeps the Stack control at the same x on every row of every
    section. Sharing is NOT here any more — the Sharing column's badge is the
    control, so a separate share icon would be a second door to one room."""
    import re

    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Slots Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    row = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    assert 'class="lib-slot lib-slot--stack"' in row
    assert "lib-slot--share" not in row
    assert "lib-iconbtn" not in text


def test_collection_rows_carry_no_type_badge(seeded_app):
    """The folder icon, the row styling and the file count already say
    "collection" — a Type chip repeating it is noise, so the cell is left empty.
    A loose file keeps its real format badge."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Badge Solo", tok)
    _upload(seeded_app, solo["id"], "b.md", tok)
    _folder(seeded_app, "Badge Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    folder = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    file_row = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S).group(0)
    assert "lib-typechip" not in folder
    assert "Collection" not in folder
    assert "lib-typechip" in file_row


def test_sharing_badge_is_the_control_when_you_own_it(seeded_app):
    """On an item the caller owns the badge IS the sharing control: a button with
    a chevron that opens the dialog. It must not double as row navigation."""
    import re

    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Editable Sharing", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    row = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    badge = re.search(r"<button[^>]*lib-vis[^>]*>", row)
    assert badge, "the owned row's sharing badge is not a button"
    assert "lib-vis--editable" in badge.group(0)
    assert f'data-share="{col["id"]}"' in badge.group(0)
    assert f"change who can see {col['name']}" in badge.group(0)
    assert "lib-vis__caret" in row  # the "editable" cue


def test_sharing_badge_is_read_only_when_shared_with_you(seeded_app):
    """An item shared WITH the caller keeps a plain badge whose tooltip says the
    owner controls access — a control that cannot act is worse than none."""
    import re

    from src.db import get_system_db
    from src.repositories import data_packages_repo
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("ro-badge-grp") or groups.create(name="ro-badge-grp", description="t", created_by="t")
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    pkg = data_packages_repo().create(
        name="RO Badge Data", slug="ro-badge-data", description="d", icon=None, color=None, created_by="admin"
    )
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="data_package", resource_id=pkg, assigned_by="admin"
    )

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = re.search(r'<tr[^>]*data-type="data_package".*?</tr>', text, re.S).group(0)
    assert "only the owner can change" in row
    assert "lib-vis--editable" not in row
    assert "lib-vis__caret" not in row
    # No share affordance at all on something you can't re-share.
    assert "data-share=" not in row
    # Its own COLOUR, not just its own tooltip: the badge must not wear the
    # primary tint the clickable sharing badges use (see --ds-readonly-* in
    # design-tokens.css). Shape alone (a missing chevron) is too quiet a signal
    # for "this one does nothing when you press it".
    assert "lib-vis--readonly" in row


def test_read_only_sharing_badge_has_its_own_colour_tokens():
    """The read-only badge's hue is a token trio, defined for light AND dark, and
    never an alias of --ds-primary — the tint that means "this is a control"."""
    from pathlib import Path

    tokens = Path("app/web/static/css/design-tokens.css").read_text(encoding="utf-8")
    library = Path("app/web/templates/library.html").read_text(encoding="utf-8")

    for name in ("--ds-readonly-bg", "--ds-readonly-ink", "--ds-readonly-line"):
        # Declared twice: the light default plus the dark-theme retint (the light
        # violet pastel washes out on the dark surface family).
        assert tokens.count(f"{name}:") >= 2, f"{name} is not defined for both light and dark"
        assert f"{name}:   var(--ds-primary" not in tokens, f"{name} must not alias the action colour"

    assert ".lib-vis--readonly" in library
    # Declared after the primary-tinted rule, or it would lose on equal specificity.
    assert library.index(".lib-vis--shared") < library.index(".lib-vis--readonly {")


def test_details_column_is_retired_and_a_collection_shows_its_contents(seeded_app):
    """The Details column is gone. A COLLECTION now says what's inside it where a
    description would go ("2 files") — more useful than boilerplate prose, and the
    only place the count still shows in the table. A file keeps its description;
    both carry the meta text on the row for the grid cards to render."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Details Solo", tok)
    _upload(seeded_app, solo["id"], "d.md", tok)
    _folder(seeded_app, "Details Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert "<th>Details</th>" not in text
    assert "lib-col-details" not in text
    # Five columns: Name · Type · Owner · Sharing · Actions — in each group's own
    # header (they are identical; check the first).
    head = re.search(r"<thead>.*?</thead>", text, re.S).group(0)
    # `<th[ >]` so the count doesn't also match the enclosing <thead>.
    assert len(re.findall(r"<th[ >]", head)) == 5

    folder = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    assert re.search(r'lib-name-desc">\s*2 files\s*<', folder), "collection does not show its contents"
    assert 'data-meta="2 files"' in folder
    loose = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S).group(0)
    assert "searchable by your agents" in loose  # a file keeps its description
    assert 'data-meta="d.md' in loose  # …and its meta rides the row


def test_added_column_is_retired(seeded_app):
    """The Added column is gone from the table — the timestamp still rides the row
    as `data-added` (the sort reads it) and shows on the grid cards' metadata
    line, but it no longer spends a column of its own."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "No Added Column", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert "<th>Added</th>" not in text
    assert "lib-col-added" not in text
    # The data the sort needs is still there.
    assert "data-added=" in text
    assert 'value="added_desc"' in text


def test_rows_carry_what_an_in_place_move_needs(seeded_app):
    """A drag or keyboard move reconciles the table in the browser instead of
    reloading, so each row has to carry the facts that rebuild it: the target's
    slug (the moved file's per-file URL), the file's own name (its title as a
    child), and the collection's file count (to bump)."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Move Hooks Solo", tok)
    _upload(seeded_app, solo["id"], "hook.md", tok)
    col = _folder(seeded_app, "Move Hooks Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    folder = re.search(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S).group(0)
    assert f'data-slug="{col["slug"]}"' in folder
    assert 'data-files="2"' in folder
    loose = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S).group(0)
    assert 'data-filename="hook.md"' in loose
    assert "data-file-id=" in loose


def test_a_movable_file_has_a_keyboard_move_control(seeded_app):
    """Dragging is pointer-only, so every draggable file also carries a real
    BUTTON whose menu lists the collections it can move into. Non-file kinds get
    neither — they can't go into a collection at all."""
    import re

    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Keyboard Move", tok)
    _upload(seeded_app, solo["id"], "kb.md", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    loose = re.search(r'<tr class="lib-row lib-row--file".*?</tr>', text, re.S).group(0)
    grip = re.search(r"<button[^>]*data-move-file[^>]*>", loose)
    assert grip, "no keyboard move control on a movable file"
    assert 'aria-haspopup="true"' in grip.group(0)
    assert "Move Keyboard Move into a collection" in grip.group(0)
    # A collection is a target, never a thing you move into one.
    folder_rows = re.findall(r'<tr class="lib-row lib-row--folder".*?</tr>', text, re.S)
    for fr in folder_rows:
        assert "data-move-file" not in fr


def test_sections_carry_their_detail_page_accent(seeded_app):
    """A type's section is coloured with the SAME --ds-kind-* token its detail
    page hero resolves, so the colour is the type's identity end to end."""
    tok = seeded_app["admin_token"]
    _folder(seeded_app, "Accent Folder", tok)

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert "--lib-kind: var(--ds-kind-library)" in text
    assert "--lib-kind-soft: var(--ds-kind-library-soft)" in text


def test_non_file_kinds_are_separate_sections_and_never_drop_targets(seeded_app):
    """Skills / data packages / memory stay top-level and can't take files."""
    import re

    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories import data_packages_repo

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("files-sec-grp") or groups.create(name="files-sec-grp", description="t", created_by="t")
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    pkg = data_packages_repo().create(
        name="Sec Data", slug="sec-data", description="d", icon=None, color=None, created_by="admin"
    )
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="data_package", resource_id=pkg, assigned_by="admin"
    )

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert 'data-lib-sec="data_package"' in text
    # The data-package row is not a folder and not a drop target.
    row = re.search(r'<tr[^>]*data-type="data_package"[^>]*>', text)
    assert row, "data_package row not found"
    assert "data-drop-target" not in row.group(0)
    assert 'data-folder="1"' not in row.group(0)


# ---------------------------------------------------------------------------
# Per-file detail page + per-file sharing
# ---------------------------------------------------------------------------


def test_file_inside_a_folder_has_its_own_detail_page(seeded_app):
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Detail Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    r = seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(tok))
    assert r.status_code == 200
    # It names the file, links back to its folder, and shows its OWN sharing.
    assert f"/library/{col['slug']}" in r.text
    assert "lfd-vis--private" in r.text
    assert "Share this file" in r.text


def test_file_detail_uses_the_shared_detail_scaffold(seeded_app):
    """The file page is a detail page like any other (data package, plugin,
    table): the shared hero + section cards from macros/_detail.html — and it
    presents the file only. No in-page ask/search box, and no upload drop-zone
    (uploading targets a COLLECTION, so it lives on the collection page)."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Scaffold Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    text = seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(tok)).text
    # Shared scaffold, not a bespoke header.
    assert "detail-hero" in text
    assert "detail-section" in text
    assert "detail-hero--compact" in text  # library item pages use the compact header
    # Presents the file: facts as detail rows, sharing as its own section.
    assert "detail-rows" in text
    assert ">Details<" in text
    assert ">Sharing<" in text
    # Neither an ask box nor an upload zone.
    assert "Ask this file" not in text
    assert "Upload files" not in text
    assert 'id="lib-drop"' not in text


def test_file_detail_leads_with_a_preview_action(seeded_app):
    """The first thing you want from a file's page is to see the file, so
    Preview is the primary hero action and opens the shared modal."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Preview Action Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    text = seeded_app["client"].get(f"/library/{col['slug']}/f/{fid}", headers=_auth(tok)).text
    assert 'id="lfd-preview-btn"' in text
    assert "js/file_preview.js" in text
    assert "css/file_preview.css" in text
    assert "openFilePreview" in text


def test_collection_rows_preview_in_place_and_stay_links(seeded_app):
    """A row click previews the file without leaving the collection — but the
    row is still a real link to the file's own page, so Cmd/middle-click, the
    context menu and a no-JS load all still work."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Row Preview Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    text = seeded_app["client"].get(f"/library/{col['slug']}", headers=_auth(tok)).text
    assert f'href="/library/{col["slug"]}/f/{fid}"' in text
    assert f'data-preview-file="{fid}"' in text
    assert f'data-preview-collection="{col["id"]}"' in text
    assert "js/file_preview.js" in text


def test_file_detail_404s_for_wrong_collection_and_unknown_file(seeded_app):
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Guard Folder", tok)
    other = _folder(seeded_app, "Other Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    c = seeded_app["client"]
    assert c.get(f"/library/{other['slug']}/f/{fid}", headers=_auth(tok)).status_code == 404
    assert c.get(f"/library/{col['slug']}/f/cf_nope", headers=_auth(tok)).status_code == 404
    # Someone with no access to the parent gets 404, not 403.
    assert c.get(f"/library/{col['slug']}/f/{fid}", headers=_auth(seeded_app["analyst_token"])).status_code == 404


def test_a_single_file_can_be_shared_without_its_folder(seeded_app):
    """The point of per-file sharing: one file goes out, the folder stays put."""
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Mixed Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]

    c = seeded_app["client"]
    gs = c.get("/api/sharing/groups", headers=_auth(tok)).json()
    everyone = next(g for g in gs if g["is_everyone"])
    r = c.put(f"/api/sharing/corpus_file/{fid}", json={"group_ids": [everyone["id"]]}, headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "workspace"
    # The folder itself is untouched.
    assert c.get(f"/api/sharing/collection/{col['id']}", headers=_auth(tok)).json()["visibility"] == "private"
    # And the Library shows the file's own state.
    text = c.get("/library", headers=_auth(tok)).text
    assert 'data-share-type="corpus_file"' in text


def test_per_file_sharing_is_owner_scoped(seeded_app):
    """A non-owner can neither read nor change a file's sharing (404)."""
    col = _folder(seeded_app, "Owned Folder", seeded_app["admin_token"])
    fid = _files(seeded_app, col["id"], seeded_app["admin_token"])[0]["file_id"]
    other = _auth(seeded_app["analyst_token"])
    c = seeded_app["client"]
    assert c.get(f"/api/sharing/corpus_file/{fid}", headers=other).status_code == 404
    assert c.put(f"/api/sharing/corpus_file/{fid}", json={"group_ids": []}, headers=other).status_code == 404


# ---------------------------------------------------------------------------
# Drag-and-drop move
# ---------------------------------------------------------------------------


def test_move_puts_a_loose_file_into_a_folder_and_tidies_the_husk(seeded_app):
    """Dragging a single-file artefact into a folder must not strand an empty
    collection in the listing — a loose file IS its collection."""
    tok = seeded_app["admin_token"]
    solo = _collection(seeded_app, "Lonely File", tok)
    _upload(seeded_app, solo["id"], "lonely.md", tok)
    folder = _folder(seeded_app, "Home Folder", tok)
    fid = _files(seeded_app, solo["id"], tok)[0]["file_id"]

    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{solo['id']}/files/{fid}/move",
        json={"target_collection_id": folder["id"]},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text
    assert r.json()["source_emptied"] is True
    assert len(_files(seeded_app, folder["id"], tok)) == 3
    # The emptied source is gone from the Library.
    assert "Lonely File" not in c.get("/library", headers=_auth(tok)).text


def test_move_between_folders_keeps_both(seeded_app):
    tok = seeded_app["admin_token"]
    src = _folder(seeded_app, "Src Folder", tok, names=("p.md", "q.md"))
    dst = _folder(seeded_app, "Dst Folder", tok, names=("r.md", "s.md"))
    fid = _files(seeded_app, src["id"], tok)[0]["file_id"]

    r = seeded_app["client"].post(
        f"/api/collections/{src['id']}/files/{fid}/move",
        json={"target_collection_id": dst["id"]},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text
    assert r.json()["source_emptied"] is False
    assert len(_files(seeded_app, src["id"], tok)) == 1
    assert len(_files(seeded_app, dst["id"], tok)) == 3


def test_move_rejects_same_collection_and_unknown_target(seeded_app):
    tok = seeded_app["admin_token"]
    col = _folder(seeded_app, "Reject Folder", tok)
    fid = _files(seeded_app, col["id"], tok)[0]["file_id"]
    c = seeded_app["client"]

    r = c.post(
        f"/api/collections/{col['id']}/files/{fid}/move", json={"target_collection_id": col["id"]}, headers=_auth(tok)
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "same_collection"

    r = c.post(
        f"/api/collections/{col['id']}/files/{fid}/move", json={"target_collection_id": "col_nope"}, headers=_auth(tok)
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "target_not_found"


def test_move_requires_access_to_the_target(seeded_app):
    """Both ends are gated — a caller can't push a file into a collection they
    can't reach (and gets 404, never a hint that it exists)."""
    admin, analyst = seeded_app["admin_token"], seeded_app["analyst_token"]
    mine = _folder(seeded_app, "Analyst Folder", analyst)
    theirs = _collection(seeded_app, "Admin Only", admin)
    fid = _files(seeded_app, mine["id"], analyst)[0]["file_id"]

    r = seeded_app["client"].post(
        f"/api/collections/{mine['id']}/files/{fid}/move",
        json={"target_collection_id": theirs["id"]},
        headers=_auth(analyst),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "target_not_found"
