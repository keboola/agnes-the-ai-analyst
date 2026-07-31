"""Web UI routes for Collections — /library and /library/{slug}."""

from __future__ import annotations

import io

# Glyph path fragments (mirror macros/_catalog_card.html kind_glyph).
_DOC_GLYPH = "M7 4h7l4 4v12H7z"  # single document — a one-file artefact
_LIB_GLYPH = "M9 7h6l4 4v9"  # two overlapping sheets — a collection (detail hero)
# In the Library's Files TABLE a collection wears a folder glyph instead: there
# it sits beside loose files and takes drops, so it reads as the container it is.
_FOLDER_GLYPH = "M4 7.5A1.5 1.5 0 0 1 5.5 6"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(seeded_app, name: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 201, r.text
    return r.json()


def _upload(seeded_app, cid: str, filename: str, content: bytes, ctype: str):
    return seeded_app["client"].post(
        f"/api/collections/{cid}/files",
        files={"files": (filename, io.BytesIO(content), ctype)},
        headers=_auth(seeded_app["admin_token"]),
    )


def test_library_page_renders_with_collections(seeded_app):
    c = seeded_app["client"]
    _create(seeded_app, "LibraryUI Demo")
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    # /library is now the unified Library surface (the renamed /artefacts), not
    # the old "Your collections" list page it replaced.
    assert "Search library" in r.text
    assert "LibraryUI Demo" in r.text


def test_library_has_no_agent_affordance(seeded_app):
    """Agents are NOT a Library kind — they have their own home at /agents, so
    the Library header offers no "Build an agent" entry and lists no agent rows.
    (Supersedes an earlier deep-link-into-the-builder assertion.)"""
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "Build an agent" not in r.text
    assert 'data-kind="agent"' not in r.text


def test_library_detail_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    col = _create(seeded_app, "DetailUI Demo")
    r = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "DetailUI Demo" in r.text
    assert "Upload files" in r.text
    # A detail page presents its entity — no in-page ask/search box (the hero's
    # Ask Agnes action carries asking into chat instead).
    assert "Ask this collection" not in r.text
    assert 'id="lib-q"' not in r.text


def test_library_detail_404_for_missing(seeded_app):
    r = seeded_app["client"].get("/library/does-not-exist", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


def test_library_detail_404_for_non_member(seeded_app):
    c = seeded_app["client"]
    col = _create(seeded_app, "Private UI")
    # analyst1 has no grant — returns 404 (not 403) so existence isn't leaked.
    r = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 404


def test_library_lists_only_accessible(seeded_app):
    c = seeded_app["client"]
    _create(seeded_app, "Hidden From Analyst")
    r = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 200
    # analyst1 has no grants → no rows for it, but the page still renders
    assert "Hidden From Analyst" not in r.text


def test_single_file_artefact_presents_as_file(seeded_app, monkeypatch):
    """One file in an artefact reads AS the file — single-document glyph,
    filename + size in the meta, "File" framing, never "a collection with 1
    file" — but the title is the artefact's NAME (what the caller typed), not
    the filename, so distinct names stay distinct (they previously all
    rendered as the same filename)."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    col = _create(seeded_app, "Solo Upload")
    _upload(seeded_app, col["id"], "report.pdf", b"%PDF-1.4 x", "application/pdf")

    lst = c.get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "Solo Upload" in lst  # the typed name is the title
    assert "report.pdf" in lst  # filename is surfaced in the meta
    assert _DOC_GLYPH in lst  # single-document glyph
    assert "1 file" not in lst  # never the "1 file" container framing

    det = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"])).text
    assert "Solo Upload" in det  # the typed name is the hero title
    assert "report.pdf" in det  # filename surfaced in the hero meta
    assert _DOC_GLYPH in det  # single-document hero glyph
    assert "Ask this file" not in det  # detail pages carry no in-page ask box


def test_multi_file_artefact_presents_as_collection(seeded_app, monkeypatch):
    """A second file promotes the artefact to a Collection: the list shows
    ``N files`` + a folder glyph (it's a container among loose files there), and
    the detail page reads as a collection again — under the two-sheet hero."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    col = _create(seeded_app, "Grouped Upload")
    _upload(seeded_app, col["id"], "a.txt", b"aaa", "text/plain")
    _upload(seeded_app, col["id"], "b.txt", b"bbb", "text/plain")

    lst = c.get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "2 files" in lst
    assert _FOLDER_GLYPH in lst  # folder glyph — the row is a drop target

    det = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"])).text
    assert "collection" in det  # collection framing in the hero copy
    assert _LIB_GLYPH in det  # two-sheet hero glyph is unchanged


def test_single_file_artefacts_with_same_filename_keep_distinct_names(seeded_app, monkeypatch):
    """Regression: two single-file artefacts holding the *same* filename but
    given different names must render under their own names on the Artefacts
    list — the title is the name, not the filename, so they don't collapse
    into two identical rows."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    a = _create(seeded_app, "First Report")
    b = _create(seeded_app, "Second Report")
    _upload(seeded_app, a["id"], "logo.png", b"\x89PNG\r\n\x1a\n x", "image/png")
    _upload(seeded_app, b["id"], "logo.png", b"\x89PNG\r\n\x1a\n x", "image/png")

    lst = c.get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "First Report" in lst
    assert "Second Report" in lst


# ── Stack pill: what a row says about how it got into the Stack ────────────
#
# Membership is ONE state — "In Stack" — and the rows differ only in whether
# the caller may change it:
#
#   Required grant   → "In Stack" + LOCK. Not the caller's to remove; the
#                      unsubscribe API answers 400 cannot_remove_required.
#                      The word "Required" is NOT the label: the tier is an
#                      attribute of the membership, filterable through the
#                      separate Optional/Required facet.
#   Available grant  → "In Stack", plain checkmark. Auto-membership
#                      (StackResolver's browse() sets in_stack
#                      unconditionally) — no "add" to offer, the grant did it.
#   Own artefact     → "In Stack" ⇄ "Add to Stack", a real toggle. A personal
#                      upload has no admin grant tier, so the subscription row
#                      IS the membership.


def _grant_package(conn, *, slug: str, name: str, user_id: str, requirement: str) -> str:
    """Seed a data package granted to the caller's group at ``requirement``."""
    import uuid

    from src.repositories.data_packages import DataPackagesRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    pkg_id = DataPackagesRepository(conn).create(
        name=name,
        slug=slug,
        description="d",
        icon=None,
        color=None,
        created_by="test",
    )
    gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
    UserGroupMembersRepository(conn).add_member(user_id, gid, source="test")
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) VALUES (?, ?, 'data_package', ?, ?, CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), gid, pkg_id, requirement],
    )
    return pkg_id


def _row_for(body: str, title: str) -> str:
    """The single <tr> carrying ``title`` — so a pill assertion can't be
    satisfied by some other row elsewhere on the page."""
    start = body.rindex("<tr", 0, body.index(title))
    return body[start : body.index("</tr>", start)]


#: The locked-membership tooltip, verbatim. A test that paraphrases it would
#: let the shipped copy drift from the spec, so the exact sentence is asserted.
LOCKED_TOOLTIP = "Required by your admin and cannot be removed from your stack."


def test_library_required_grant_is_locked_in_stack(seeded_app):
    """A required grant reads the SAME "In Stack" as any other member — it is
    one — and is marked by a lock plus the locked tooltip. The tier is an
    attribute of the membership, not a different state, so the word "Required"
    is NOT the pill's label (the separate Optional/Required facet filters it)."""
    from src.db import get_system_db

    conn = get_system_db()
    _grant_package(conn, slug="req-pkg", name="Mandated Package", user_id="analyst1", requirement="required")
    conn.close()

    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row_for(body, "Mandated Package")
    assert "In Stack" in row
    assert "lib-instack--required" in row  # locked → lock glyph + info tint
    assert LOCKED_TOOLTIP in row
    # Not a button, and not addable — nothing to click either way.
    assert "data-remove-from-stack" not in row
    assert "data-add-to-stack" not in row
    # It still FILTERS as in-stack: a locked membership IS a membership, so the
    # "In stack only" toggle keeps it (the tier is filterable on its own
    # Optional/Required category).
    assert 'data-stack="in_stack"' in row


def test_library_available_grant_reads_in_stack_and_offers_no_toggle(seeded_app):
    """An available grant is already in the stack by auto-membership, so the
    row reports that plainly — no "Add to Stack" for something already in it,
    and no remove (the grant is an admin's to change)."""
    from src.db import get_system_db

    conn = get_system_db()
    _grant_package(conn, slug="avail-pkg", name="Offered Package", user_id="analyst1", requirement="available")
    conn.close()

    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row_for(body, "Offered Package")
    assert "In Stack" in row
    assert "lib-instack--fixed" in row
    assert "lib-instack--required" not in row  # not mandated — plain membership
    assert "data-add-to-stack" not in row
    assert "data-remove-from-stack" not in row


def test_library_lists_granted_curated_plugins(seeded_app):
    """Regression: a granted curated marketplace plugin must appear in the
    Library's Plugins section.

    The grant's ``resource_id`` is ``"<marketplace_id>/<plugin_name>"`` — the
    key the API is gated on. The Library used to rebuild that path through a
    ``{registry_id: row["slug"]}`` map, but ``marketplace_registry`` has no
    ``slug`` column (its PRIMARY KEY *is* the slug), so the path came out
    ``"None/<plugin>"``, matched no grant, and dropped EVERY curated plugin —
    silently, since a non-matching path raises nothing. `/catalog` still
    showed them (it reads plugin *subscriptions*, not grants), so the two
    surfaces disagreed about what the caller had.
    """
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url) VALUES (?, 'Curated Co', 'https://example.com/r.git')",
        ["curated-lib-test"],
    )
    conn.execute(
        "INSERT INTO marketplace_plugins (marketplace_id, name, description, is_system) "
        "VALUES (?, ?, 'A granted curated plugin', FALSE)",
        ["curated-lib-test", "granted-plugin"],
    )
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("plugin-grant-grp") or groups.create(
        name="plugin-grant-grp", description="t", created_by="t"
    )
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"],
        resource_type="marketplace_plugin",
        # Exactly the key the API gates on — NOT a registry slug lookup.
        resource_id="curated-lib-test/granted-plugin",
        assigned_by="admin",
    )
    conn.close()

    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "granted-plugin" in body, "granted curated plugin missing from the Library"
    row = _row_for(body, "granted-plugin")
    # Granted, so it reports membership and offers no toggle.
    assert "In Stack" in row
    assert "data-add-to-stack" not in row


def test_library_own_artefact_keeps_a_real_stack_toggle(seeded_app):
    """The contrast case: an artefact's membership IS the caller's, so its
    pill stays an actionable button rather than a status."""
    c = seeded_app["client"]
    col = _create(seeded_app, "Toggleable Artefact")

    row = _row_for(c.get("/library", headers=_auth(seeded_app["admin_token"])).text, "Toggleable Artefact")
    assert "data-add-to-stack" in row  # not yet added
    assert "lib-instack--required" not in row

    r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code in (200, 201), r.text

    row = _row_for(c.get("/library", headers=_auth(seeded_app["admin_token"])).text, "Toggleable Artefact")
    assert "data-remove-from-stack" in row  # …and removable once added
    assert "lib-instack--fixed" not in row
