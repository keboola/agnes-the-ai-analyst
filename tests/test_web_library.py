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


def _seed_definitions(metrics: int = 1, terms: int = 1) -> None:
    """Put a metric and/or a glossary term into the semantic layer.

    The Definitions footer only renders when at least one side is populated, so
    a test that wants it has to say so — which is the point of the guard.
    """
    from src.repositories import glossary_repo, metric_repo

    for i in range(metrics):
        metric_repo().create(
            id=f"finance/m{i}",
            name=f"m{i}",
            display_name=f"Metric {i}",
            category="finance",
            sql="SELECT 1",
            description="A canonical definition.",
        )
    for i in range(terms):
        glossary_repo().create(id=f"g{i}", term=f"Term {i}", definition="What it means here.")


def test_library_shows_definitions_as_a_footer_not_a_row(seeded_app):
    """The semantic layer closes the page; it is NOT part of the inventory.

    It shipped briefly as a "Definitions" band holding two rows, and that was
    wrong: metrics and glossary terms are the one thing here nobody owns,
    shares, installs, drops or edits, so as rows they had to blank all four of
    the table's columns at once (Owner / Sharing / Stack / Actions). Four
    special-cased columns is the list saying the object is not one of its rows.
    A data package looks similar but differs where it counts — access to it
    varies per caller, which is what makes it "what I have"; everyone has the
    whole glossary.
    """
    _seed_definitions(metrics=2, terms=3)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="lib-defs"' in r.text
    assert "2 metrics" in r.text
    assert "3 glossary terms" in r.text
    assert 'href="/catalog/semantics#metrics"' in r.text
    assert 'href="/catalog/semantics#glossary"' in r.text
    # Not inventory: no band, no row, no kind.
    assert 'data-lib-sec="definitions"' not in r.text
    assert 'data-kind="definitions"' not in r.text
    # And not the retired header link either.
    assert "lib-browse-semantics" not in r.text


def test_library_definitions_footer_carries_its_contents_for_search(seeded_app):
    """The footer is searchable BY TERM, not just by the word "definitions".

    Someone types "MRR" or "active account" — the term they half-remember —
    the list comes back empty, and the footer is the one thing on the page
    that knows the word. Without the index it stays silent and the reader
    concludes Agnes has never heard of it. This is also the only one-step term
    lookup the rail chrome has, since it renders no global search box.
    """
    from src.repositories import glossary_repo, metric_repo

    metric_repo().create(
        id="finance/mrr",
        name="mrr",
        display_name="Monthly Recurring Revenue",
        category="finance",
        sql="SELECT 1",
        description="Normalized monthly subscription revenue.",
        synonyms=["ARR"],
    )
    glossary_repo().create(id="g_active", term="Active account", definition="At least one paid seat.")

    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    index = r.text.split('data-defs-search="', 1)[1].split('"', 1)[0]
    # Reachable by display name, short name, synonym and glossary term.
    assert "monthly" in index and "recurring" in index
    assert "mrr" in index
    assert "arr" in index
    assert "active" in index
    # Definition BODIES stay out — the index ships on every page load, and
    # matching on prose would surface the block on incidental words.
    assert "normalized" not in index
    assert "paid" not in index


def test_library_definitions_footer_counts_are_singular_for_one(seeded_app):
    """ "1 metric", not "1 metrics" — the count is read as a sentence."""
    _seed_definitions(metrics=1, terms=1)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "1 metric " in r.text or "1 metric&" in r.text or "1 metric<" in r.text
    assert "1 metrics" not in r.text
    assert "1 glossary terms" not in r.text


def test_library_hides_definitions_footer_when_semantic_layer_is_empty(seeded_app):
    """No metrics AND no glossary -> no footer.

    A block advertising "0 metrics · 0 glossary terms" describes the instance's
    setup, not its content, and reads as a broken feature rather than an
    unconfigured one. One populated side is enough to render it.
    """
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="lib-defs"' not in r.text
    assert "/catalog/semantics" not in r.text


def test_library_shows_definitions_footer_when_only_one_side_is_populated(seeded_app):
    """Metrics but no glossary still renders it — "0 glossary terms" is a true
    and useful statement about a semantic layer that exists."""
    _seed_definitions(metrics=1, terms=0)
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert 'id="lib-defs"' in r.text
    assert "0 glossary terms" in r.text


def test_library_definitions_footer_is_labeled_in_plain_words(seeded_app):
    """ "Definitions", never "Semantic layer" — that name belongs to
    /admin/semantic-layer, where the reader operates the sync rather than
    looking a term up. Scoped to the footer because an admin's nav dropdown
    carries the admin link on every page, and that one is correct."""
    _seed_definitions()
    c = seeded_app["client"]
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    block = r.text.split('id="lib-defs"', 1)[1].split("</aside>", 1)[0]
    assert "Definitions" in block
    assert "Semantic layer" not in block


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


#: The locked-membership tooltips, verbatim. A test that paraphrases them would
#: let the shipped copy drift from the spec, so the exact sentences are asserted.
#: Both tiers are locked; only the wording differs.
LOCKED_TOOLTIP = "Required by your admin and cannot be removed from your stack."
GRANTED_TOOLTIP = "Granted to your group — only an admin can remove it from your Stack."


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
    assert "lib-instack--locked" in row  # locked → lock glyph + info tint
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
    and no remove (the grant is an admin's to change).

    It is LOCKED too, for the same reason the required one is: the grant IS the
    membership, so there is nothing on the row to drop. The lock is keyed on
    droppability, NOT on the tier — keyed on the tier, this row wore the
    success-tinted check that a *removable* pill shows at rest, and the only
    way to find out it wasn't one was to hover it and watch nothing happen.
    The tier lives in the tooltip and in the Optional/Required facet."""
    from src.db import get_system_db

    conn = get_system_db()
    _grant_package(conn, slug="avail-pkg", name="Offered Package", user_id="analyst1", requirement="available")
    conn.close()

    body = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row_for(body, "Offered Package")
    assert "In Stack" in row
    assert "lib-instack--fixed" in row
    assert "lib-instack--locked" in row  # not the removable pill's rest state
    assert "data-add-to-stack" not in row
    assert "data-remove-from-stack" not in row
    # …and it says who CAN remove it, rather than only what it is.
    assert GRANTED_TOOLTIP in row
    # The tier is still distinguishable — just not by the affordance.
    assert LOCKED_TOOLTIP not in row


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
    assert "lib-instack--locked" not in row

    r = c.post(f"/api/stack/artefacts/{col['id']}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code in (200, 201), r.text

    row = _row_for(c.get("/library", headers=_auth(seeded_app["admin_token"])).text, "Toggleable Artefact")
    assert "data-remove-from-stack" in row  # …and removable once added
    assert "lib-instack--fixed" not in row
    assert "lib-instack--locked" not in row
