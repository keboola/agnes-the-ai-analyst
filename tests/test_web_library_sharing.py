"""Library sharing + the agent registry (v103).

Three things ship together here and are locked in one place:

  - ``/api/agents`` — the server-side agent registry that replaced the Agent
    builder's localStorage-only store, so an agent is a real Library item.
  - ``/api/sharing`` — OWNER-initiated sharing. Everything in
    ``app/api/access.py`` is ``require_admin``; this is the counterpart that
    lets the creator of an item share it with groups they belong to. The
    security-relevant invariants are ownership and group containment.
  - ``/library`` — the renamed, widened former ``/artefacts``, listing
    artefacts + skills with per-row visibility. Agents are deliberately NOT
    listed there (they have their own home at ``/agents``), but they remain
    real registry rows whose grants ``/api/agents`` honours.

Skills are deliberately NOT grant-shareable: an approved store entity is
already readable by every authenticated user, so a grant row on one would be
read by nothing. ``/api/sharing/skill/...`` must 404 rather than pretend.
"""

from __future__ import annotations

import re


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_collection(seeded_app, name: str, token: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _create_agent(seeded_app, token: str, **fields) -> dict:
    payload = {"name": "Test Agent"}
    payload.update(fields)
    r = seeded_app["client"].post("/api/agents", json=payload, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _group_with_member(user_id: str, group_name: str) -> str:
    """Create ``group_name`` (if absent) and put ``user_id`` in it."""
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    return grp["id"]


def _bare_group(group_name: str) -> str:
    """A group the caller is NOT a member of."""
    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository

    groups = UserGroupsRepository(get_system_db())
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    return grp["id"]


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------


def test_agent_create_list_patch_delete_roundtrip(seeded_app):
    c = seeded_app["client"]
    tok = seeded_app["admin_token"]

    a = _create_agent(seeded_app, tok, name="Revenue Analyst", role="Finance", knowledge=["col_x"])
    assert a["id"].startswith("agt_")
    assert a["slug"] == "revenue-analyst"
    assert a["mine"] is True
    # Web chat is the always-on baseline surface.
    assert a["surfaces"]["web"] is True

    listed = c.get("/api/agents", headers=_auth(tok)).json()["agents"]
    assert any(x["id"] == a["id"] for x in listed)

    patched = c.patch(f"/api/agents/{a['id']}", json={"name": "Renamed", "plugins": ["p1"]}, headers=_auth(tok))
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["plugins"] == ["p1"]

    assert c.delete(f"/api/agents/{a['id']}", headers=_auth(tok)).status_code == 204
    assert c.get(f"/api/agents/{a['id']}", headers=_auth(tok)).status_code == 404


def test_agent_slug_collision_gets_suffix_not_conflict(seeded_app):
    """Two agents may share a name — duplicates are ordinary for user-named
    things, so the second gets `-2` rather than a 409."""
    tok = seeded_app["admin_token"]
    first = _create_agent(seeded_app, tok, name="Analyst")
    second = _create_agent(seeded_app, tok, name="Analyst")
    assert first["slug"] == "analyst"
    assert second["slug"] == "analyst-2"


def test_agent_slug_freed_name_reuses_suffix_after_delete(seeded_app):
    """Deleting an agent must not poison its slug for the next one.

    ``delete`` is a SOFT delete (``deleted_at``), but the ``slug`` UNIQUE
    constraint spans deleted rows on both backends — so the free-slug search
    has to see them. It previously used the default (live-rows-only) lookup,
    which reported the freed slug as available and drove the INSERT into
    ``ConstraintException`` → 500. Create-delete-create is the ordinary path
    for an untitled draft (the "+ Build an agent" button mints one every
    click), so this fired on the second click of a fresh workspace."""
    c = seeded_app["client"]
    tok = seeded_app["admin_token"]

    first = _create_agent(seeded_app, tok, name="Recycled")
    assert first["slug"] == "recycled"
    assert c.delete(f"/api/agents/{first['id']}", headers=_auth(tok)).status_code == 204

    # 201, not 500 — and the slug steps around the soft-deleted row.
    second = _create_agent(seeded_app, tok, name="Recycled")
    assert second["slug"] == "recycled-2"

    # The unnamed-draft case the builder actually hits (slug falls back to
    # "agent"), twice over, with a delete in between.
    d1 = _create_agent(seeded_app, tok, name="")
    assert c.delete(f"/api/agents/{d1['id']}", headers=_auth(tok)).status_code == 204
    d2 = _create_agent(seeded_app, tok, name="")
    assert d1["slug"] != d2["slug"]


def test_agent_default_cannot_be_deleted(seeded_app):
    """The seeded default agent is not deletable through the builder.

    It is listed like any other agent (``list_for_user`` returns it first), so
    the Library's delete control reaches it. Deleting it used to break web chat
    outright: every session create resolves the default first, and that lookup
    re-inserted a row whose ``slug='default'`` still collided with the
    soft-deleted tombstone — a permanent 500 on ``POST /api/chat/sessions``.
    The repository now revives the tombstone instead of raising, but the delete
    still has no business succeeding: the agent would vanish from the Library
    and silently reappear on the owner's next chat. `/api/v1/agents` has
    refused this since the agent-as-API work; the builder router must match.
    """
    from src.repositories import agents_repo

    c = seeded_app["client"]
    tok = seeded_app["admin_token"]

    default_id = agents_repo().get_or_create_default("admin1")["id"]
    listed = c.get("/api/agents", headers=_auth(tok)).json()["agents"]
    assert any(x["id"] == default_id for x in listed), "default agent is reachable in the Library"

    r = c.delete(f"/api/agents/{default_id}", headers=_auth(tok))
    assert r.status_code == 400
    assert r.json()["detail"] == "default_agent_undeletable"

    # Still live, and still the default.
    assert agents_repo().get_by_id(default_id)["deleted_at"] is None
    assert c.get(f"/api/agents/{default_id}", headers=_auth(tok)).status_code == 200


def test_agent_patch_cannot_reassign_ownership(seeded_app):
    """A hostile payload can't move an agent to another owner or hijack a slug."""
    tok = seeded_app["admin_token"]
    a = _create_agent(seeded_app, tok, name="Owned")
    r = seeded_app["client"].patch(
        f"/api/agents/{a['id']}",
        json={"created_by": "analyst1", "slug": "hijacked", "id": "agt_evil", "name": "Still Mine"},
        headers=_auth(tok),
    )
    assert r.status_code == 200
    assert r.json()["created_by"] == a["created_by"]
    assert r.json()["slug"] == a["slug"]
    assert r.json()["name"] == "Still Mine"


def test_agent_is_private_to_owner_until_shared(seeded_app):
    """Another user can neither list nor read an unshared agent (404, so the
    endpoint never confirms it exists)."""
    a = _create_agent(seeded_app, seeded_app["admin_token"], name="Secret Bot")
    other = _auth(seeded_app["analyst_token"])
    assert seeded_app["client"].get(f"/api/agents/{a['id']}", headers=other).status_code == 404
    listed = seeded_app["client"].get("/api/agents", headers=other).json()["agents"]
    assert all(x["id"] != a["id"] for x in listed)


def test_shared_agent_becomes_readable_but_not_writable(seeded_app):
    """A grant conveys USE, not authorship: the grantee can read the agent but
    may not edit or delete it. This is also what makes agent grants real rather
    than decorative — the read path honours them."""
    c = seeded_app["client"]
    a = _create_agent(seeded_app, seeded_app["admin_token"], name="Team Bot")
    gid = _group_with_member("analyst1", "lib-agent-share-grp")

    r = c.put(f"/api/sharing/agent/{a['id']}", json={"group_ids": [gid]}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "shared"

    other = _auth(seeded_app["analyst_token"])
    assert c.get(f"/api/agents/{a['id']}", headers=other).status_code == 200
    listed = c.get("/api/agents", headers=other).json()["agents"]
    assert any(x["id"] == a["id"] and x["mine"] is False for x in listed)
    # Read-only for the grantee.
    assert c.patch(f"/api/agents/{a['id']}", json={"name": "Hijack"}, headers=other).status_code == 404
    assert c.delete(f"/api/agents/{a['id']}", headers=other).status_code == 404


# ---------------------------------------------------------------------------
# Sharing API
# ---------------------------------------------------------------------------


def test_share_targets_include_everyone(seeded_app):
    r = seeded_app["client"].get("/api/sharing/groups", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    targets = r.json()
    assert targets, "expected at least the Everyone group"
    everyone = [t for t in targets if t["is_everyone"]]
    assert len(everyone) == 1
    assert "workspace" in everyone[0]["name"].lower()


def test_collection_share_cycles_private_shared_workspace(seeded_app):
    """The three visibility states are reachable and reversible through one
    idempotent PUT, for the artefact kind."""
    from src.db import SYSTEM_EVERYONE_GROUP

    c = seeded_app["client"]
    tok = seeded_app["admin_token"]
    col = _create_collection(seeded_app, "Shareable Deck", tok)

    assert c.get(f"/api/sharing/collection/{col['id']}", headers=_auth(tok)).json()["visibility"] == "private"

    gid = _group_with_member("analyst1", "lib-col-share-grp")
    r = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": [gid]}, headers=_auth(tok))
    assert r.json()["visibility"] == "shared"

    everyone = _bare_group(SYSTEM_EVERYONE_GROUP)
    r = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": [everyone]}, headers=_auth(tok))
    assert r.json()["visibility"] == "workspace"

    # Empty list = private again.
    r = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": []}, headers=_auth(tok))
    assert r.json()["visibility"] == "private"
    assert r.json()["group_ids"] == []


def test_share_is_idempotent(seeded_app):
    c = seeded_app["client"]
    tok = seeded_app["admin_token"]
    col = _create_collection(seeded_app, "Idem Deck", tok)
    gid = _group_with_member("analyst1", "lib-idem-grp")
    first = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": [gid]}, headers=_auth(tok)).json()
    second = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": [gid]}, headers=_auth(tok)).json()
    assert first["group_ids"] == second["group_ids"] == [gid]


def test_non_owner_cannot_read_or_change_sharing(seeded_app):
    """Sharing state of someone else's item is 404 — not 403 — so ownership
    can't be probed."""
    c = seeded_app["client"]
    col = _create_collection(seeded_app, "Not Yours", seeded_app["admin_token"])
    other = _auth(seeded_app["analyst_token"])
    assert c.get(f"/api/sharing/collection/{col['id']}", headers=other).status_code == 404
    assert c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": []}, headers=other).status_code == 404


def test_cannot_share_into_a_group_you_are_not_in(seeded_app):
    """Group containment: a non-admin owner may only target their own groups,
    so they can't push content at a team they aren't part of."""
    c = seeded_app["client"]
    tok = seeded_app["analyst_token"]
    col = _create_collection(seeded_app, "Analyst Own Deck", tok)
    foreign = _bare_group("lib-foreign-grp")
    r = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": [foreign]}, headers=_auth(tok))
    assert r.status_code == 403
    assert r.json()["detail"] == "group_not_shareable"


def test_owner_unshare_preserves_an_admin_grant(seeded_app):
    """An owner clearing their own sharing must not revoke a grant an admin
    made to a group the owner isn't in."""
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository

    c = seeded_app["client"]
    tok = seeded_app["analyst_token"]
    col = _create_collection(seeded_app, "Admin Granted Deck", tok)
    admin_grp = _bare_group("lib-admin-only-grp")
    grants = ResourceGrantsRepository(get_system_db())
    grants.create(group_id=admin_grp, resource_type="collection", resource_id=col["id"], assigned_by="admin")

    # The owner sets their own (empty) desired state.
    r = c.put(f"/api/sharing/collection/{col['id']}", json={"group_ids": []}, headers=_auth(tok))
    assert r.status_code == 200
    # The admin's grant survives, so the item is still shared.
    assert admin_grp in r.json()["group_ids"]
    assert r.json()["visibility"] == "shared"


def test_skill_is_not_grant_shareable(seeded_app):
    """Skills share via the Store (approved => readable by everyone), so the
    grant endpoint must refuse them instead of writing a dead grant row."""
    r = seeded_app["client"].get("/api/sharing/skill/anything", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404
    assert r.json()["detail"] == "resource_not_shareable"


def test_sharing_unknown_resource_is_404(seeded_app):
    r = seeded_app["client"].get("/api/sharing/agent/agt_missing", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# The Library page
# ---------------------------------------------------------------------------


def test_library_lists_artefacts_with_visibility_but_not_agents(seeded_app):
    """One table for the caller's things — artefacts (and skills) with a type
    facet and a visibility chip. Agents are excluded: they live on /agents."""
    tok = seeded_app["admin_token"]
    _create_collection(seeded_app, "Quarterly Deck", tok)
    _create_agent(seeded_app, tok, name="Library Bot")

    text = seeded_app["client"].get("/library", headers=_auth(tok)).text
    assert "Quarterly Deck" in text
    assert 'data-kind="artefact"' in text
    # Visibility chip + the share affordance for the grant-backed artefact kind.
    assert "lib-vis--private" in text
    assert 'data-share-type="collection"' in text
    # The agent exists in the registry but is NOT a Library row.
    assert "Library Bot" not in text
    assert 'data-kind="agent"' not in text


def test_shared_agent_visibility_is_reported_by_the_sharing_api(seeded_app):
    """Agents aren't Library rows, so their shared state is asserted where it
    actually surfaces: the sharing API (and, for use, /api/agents)."""
    tok = seeded_app["admin_token"]
    a = _create_agent(seeded_app, tok, name="Shared Bot")
    gid = _group_with_member("analyst1", "lib-vis-grp")
    seeded_app["client"].put(f"/api/sharing/agent/{a['id']}", json={"group_ids": [gid]}, headers=_auth(tok))

    state = seeded_app["client"].get(f"/api/sharing/agent/{a['id']}", headers=_auth(tok)).json()
    assert state["visibility"] == "shared"
    assert gid in state["group_ids"]


def test_agent_shared_with_me_is_not_in_my_library(seeded_app):
    """A shared agent is usable via /api/agents but must not leak into the
    grantee's Library listing."""
    a = _create_agent(seeded_app, seeded_app["admin_token"], name="Borrowed Bot")
    gid = _group_with_member("analyst1", "lib-borrow-grp")
    seeded_app["client"].put(
        f"/api/sharing/agent/{a['id']}", json={"group_ids": [gid]}, headers=_auth(seeded_app["admin_token"])
    )
    other = _auth(seeded_app["analyst_token"])
    listed = seeded_app["client"].get("/api/agents", headers=other).json()["agents"]
    assert any(x["id"] == a["id"] for x in listed)
    assert "Borrowed Bot" not in seeded_app["client"].get("/library", headers=other).text


def test_library_offers_grid_view_toggle(seeded_app):
    """Table is the default view; a grid toggle + its projection target ship
    with the page (the shared .fbar-view control, as on My Stack)."""
    _create_collection(seeded_app, "View Toggle Deck", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert 'class="fbar-view"' in text
    assert 'data-view="table"' in text and 'data-view="grid"' in text
    assert 'aria-pressed="true"' in text  # table active by default
    # ONE table for the whole list (the groups are its tbodies), and a parallel
    # grid container holding one grid per group — a grid cannot nest in a tbody,
    # so the two views are separate structures wearing the same group headers.

    # One table + one grid per GROUP, both inside the group's own section, so the
    # view toggle and the grouping compose (there is no page-level pair).
    groups = re.findall(r'<section class="fbar-group lib-group[^"]*" data-lib-sec="([^"]+)"', text)
    assert groups
    assert text.count('class="lib-tablewrap"') == len(groups)
    assert text.count('class="fbar-grid"') == len(groups)


def test_library_add_actions_live_behind_one_menu(seeded_app):
    """A single "+ Add" chevron button fronts every add path; there is no
    separate per-kind button in the header, and no agent entry."""
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert 'id="lib-new-btn"' in text
    assert 'id="lib-new-menu"' in text
    for label in ("Build a skill", "Build a plugin", "Upload a file"):
        assert f"<span>{label}</span>" in text
    assert "Build an agent" not in text
    # Every row goes to the one builder at /skills, so no row is marked WIP.
    assert "lib-wip" not in text
    # The connect banner is a page-level note under the header, never inside
    # the header row itself.
    assert 'class="cbn cbn--bar"' in text
    head = text.split('class="lib-head"', 1)[1].split("</div>", 1)[0]
    assert 'class="cbn cbn--bar"' not in head


def test_search_and_new_ride_the_page_header(seeded_app):
    """The two WHOLE-PAGE controls — search and "+ Add" — sit in the page header
    on the title's row, not in the toolbar and not on the count row: neither one
    narrows or reorders the list the way the toolbar's controls do."""
    # One item, so the type sections actually render — they are what bounds the
    # count row below (an empty Library renders no `data-lib-sec`).
    _create_collection(seeded_app, "Row Anchor", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text

    # Both live inside `.lib-head`, which the connect banner closes — so both
    # land ahead of the banner, and everything below the header comes after it.
    head = text.split('class="lib-head"', 1)[1].split('class="cbn cbn--bar"', 1)[0]
    assert 'id="lib-search"' in head
    assert 'id="lib-new-btn"' in head
    assert 'id="lib-new-menu"' in head

    # The toolbar keeps every list control and no search box, and both its rows
    # centre now that the one flex-grow child has left. Bounded by the dock's
    # own closing markup rather than by the chips row — the chips sit ABOVE the
    # bar, so slicing to `id="lib-chips"` would run to the end of the document
    # and assert nothing.
    assert 'class="fbar fbar--center"' in text
    assert 'class="fbar-chips fbar-chips--center"' in text
    bar = text.split('class="fbar fbar--center"', 1)[1].split("</div></div>", 1)[0]
    assert 'id="lib-search"' not in bar
    for kept in ('id="lib-filter-btn"', 'id="lib-sort"', 'class="fbar-view"'):
        assert kept in bar

    # The count row carries the count alone.
    row = text.split('class="lib-section-head"', 1)[1].split("data-lib-sec=", 1)[0]
    assert 'id="lib-item-count"' in row
    assert 'id="lib-new-btn"' not in row

    # Page order: header → dock → count row → groups, and INSIDE the dock the
    # applied-filter chips come before the controls that produced them.
    assert text.index('class="lib-head"') < text.index('class="lib-dock"')
    assert text.index('class="lib-dock"') < text.index('id="lib-chips"')
    assert text.index('id="lib-chips"') < text.index('class="fbar fbar--center"')
    assert text.index('class="fbar fbar--center"') < text.index('class="lib-section-head"')
    assert text.index('class="lib-section-head"') < text.index("data-lib-sec=")


def test_toolbar_is_a_floating_bottom_dock(seeded_app):
    """The filter/sort controls ride a fixed card pinned to the bottom of the
    viewport, with the applied-filter chips as its TOP row.

    Four things make that work and are worth pinning, because each one silently
    breaks something a caller can see:

      * both rows live in ONE card (`.lib-dock__card`), chips first;
      * the list reserves room below itself, or the dock covers its last rows;
      * every menu the dock opens is re-anchored upward — a menu still hanging
        off `top: 100%` renders below the viewport floor;
      * `.lib-actions` keeps a reserved height, since the script moves its two
        controls into the dock and observes the box they left behind.
    """
    _create_collection(seeded_app, "Dock Anchor", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text

    # One card, chips row first, controls row second.
    card = text.split('class="lib-dock__card"', 1)[1].split('class="lib-section-head"', 1)[0]
    assert card.index('id="lib-chips"') < card.index('class="fbar fbar--center"')

    css = text.split("{% endblock %}", 1)[0]
    assert "position: fixed;" in text.split(".lib-dock {", 1)[1].split("}", 1)[0]
    # Room below the list for the card to float over.
    assert "padding-bottom: 132px" in css
    # Upward-opening menus, both of them.
    assert ".lib-dock .fbar-menu {" in css and ".lib-dock .lib-new__menu {" in css
    for rule in (".lib-dock .fbar-menu {", ".lib-dock .lib-new__menu {"):
        block = css.split(rule, 1)[1].split("}", 1)[0]
        assert "top: auto;" in block
        assert "bottom: calc(100%" in block
    # The header keeps the row its two controls leave behind.
    assert "min-height: 38px" in css.split(".lib-actions {", 1)[1].split("}", 1)[0]


def test_dock_card_hugs_its_controls_in_every_state(seeded_app):
    """The card is sized by what is in it, always.

    It used to take a definite `min(900px, 100%)` once search and "+ Add" docked
    into it, and push those two onto its edges with a pair of auto margins, so
    the list controls would stay on the card's centre line. With a handful of
    middle controls that left ~300px of free space for the margins to absorb and
    the dock rendered as a wide empty bar with a button stranded at each end. A
    floating card has nothing beside it to line up with, so there is no centre
    line to hold — only one consistent gap between its own controls.

    Keeping it intrinsic is also what gives the resize animation something to
    interpolate: with a definite width most state changes moved nothing."""
    _create_collection(seeded_app, "Dock Hug", seeded_app["admin_token"])
    css = (
        seeded_app["client"]
        .get("/library", headers=_auth(seeded_app["admin_token"]))
        .text.split("{% endblock %}", 1)[0]
    )

    # No definite width in the docked state, and no arms to pad it out.
    assert ".lib-dock.has-page-actions .lib-dock__card { width:" not in css
    for gap_maker in (
        ".lib-dock .lib-head__search { margin-right: auto; }",
        ".lib-dock .lib-new { margin-left: auto;",
    ):
        assert gap_maker not in css, f"auto margin reopens the whitespace hole: {gap_maker}"
    # "+ Add" is content-sized, not given the search's basis.
    assert ".lib-dock .lib-new { flex: 0 0 auto; }" in css

    # The card still animates between the shapes it hugs into.
    assert "animating = true" in css or "libDockCard.animate(" in css


def test_library_title_carries_no_setup_caveat(seeded_app):
    """The whole-Library "content being prepared" caveat is gone: no `.pnote`
    panel under the lede AND no status pill on the title. The page opens on its
    own name and inventory — a standing condition that asks nothing of the reader
    does not earn a permanent marker next to the h1."""
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    # Neither the panel nor the pill that replaced it — markup, CSS and JS.
    assert 'class="pnote"' not in text
    assert "lib-status" not in text
    # None of the caveat's copy survives anywhere on the page.
    assert "Content being prepared" not in text
    assert "Library content is still being prepared" not in text
    assert "while we finish adding content" not in text
    # The title stands alone, directly ahead of the lede.
    assert "<h1>Library</h1>" in text
    assert text.index("<h1>Library</h1>") < text.index('class="lede"')


def test_data_apps_note_is_an_info_banner_below_the_list(seeded_app):
    """The Data apps caveat is an INFO BANNER under the inventory — not a `.pnote`
    in the page head (which would open the page on the one kind you cannot have),
    and no longer a band inside `.lib-list` (which dressed a roadmap note in the
    chrome of the inventory, so it read as a sixth openable section)."""
    _create_collection(seeded_app, "Soon Banner Anchor", seeded_app["admin_token"])
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert 'class="lib-soon__badge">Coming soon<' in text
    assert 'aria-label="Data apps — coming soon"' in text
    # The old notice's copy in full — including "Nothing to do yet", which the
    # badge alone cannot say.
    assert "Hosted apps that run next to your data will live here too." in text
    assert "This is still in the works" in text
    assert "link an existing one" in text
    assert "Nothing to do yet." in text
    # Below the list, OUTSIDE its frame: `#lib-noresults` is the first thing after
    # the list's closing tag, so landing after it is what puts the banner outside.
    assert (
        text.index('class="lede"')
        < text.index('class="lib-list"')
        # every group section opens before it (the class attribute, so the page's
        # own `.lib-group` CSS and JS further down don't count)
        < text.rindex('class="fbar-group lib-group')
        < text.index('id="lib-noresults"')
        < text.index('class="lib-soon"')
    )
    # One banner, not one per page state: the stocked and empty branches share it.
    assert text.count('class="lib-soon"') == 1
    # Not inventory: no section key, no count, no collapse toggle.
    banner = text[text.index('class="lib-soon"') :]
    banner = banner[: banner.index("</div>")]
    for group_machinery in ("data-lib-sec", "lib-group", "data-sec-toggle", "data-sec-count"):
        assert group_machinery not in banner
    # It states its genre with the info vocabulary + a leading rule no band or
    # card on this page wears, and drops the band chrome it used to borrow (the
    # glyph-column inset that lined it up with the five real sections).
    assert "background: var(--ds-info-bg);" in text
    assert "border-left: 3px solid var(--ds-info-line);" in text
    assert "padding: 11px 16px 11px 42px" not in text
    # The loud page-local banner it replaced is gone, markup and CSS both.
    assert "lib-apps" not in text


# ---------------------------------------------------------------------------
# The Library also lists what has been SHARED WITH the caller
# ---------------------------------------------------------------------------


def _grant(group_id: str, resource_type: str, resource_id: str) -> None:
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository

    grants = ResourceGrantsRepository(get_system_db())
    if not grants.has_grant([group_id], resource_type, resource_id):
        grants.create(group_id=group_id, resource_type=resource_type, resource_id=resource_id, assigned_by="admin")


def test_library_lists_granted_resources_of_every_kind(seeded_app):
    """The Library answers "what do I have?" across kinds: the caller's own
    artefacts PLUS the governed data packages, memory domains and recipes
    granted to one of their groups."""
    from src.repositories import data_packages_repo, memory_domains_repo, recipes_repo

    gid = _group_with_member("analyst1", "lib-access-grp")
    pkg = data_packages_repo().create(
        name="Sales Data",
        slug="lib-sales",
        description="Governed sales tables",
        icon=None,
        color=None,
        created_by="admin",
    )
    dom = memory_domains_repo().create(
        name="Pricing Memory",
        slug="lib-pricing",
        description="How we price",
        icon=None,
        color=None,
        created_by="admin",
    )
    rec = recipes_repo().create(
        slug="lib-churn",
        title="Churn Recipe",
        description="How to compute churn",
        icon=None,
        color=None,
        sql_template=None,
        related_table_ids=None,
        created_by="admin",
    )
    _grant(gid, "data_package", pkg)
    _grant(gid, "memory_domain", dom)
    _grant(gid, "recipe", rec)

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Sales Data" in text
    assert "Pricing Memory" in text
    assert "Churn Recipe" in text
    for type_key in ("data_package", "memory_domain", "recipe"):
        assert f'data-type="{type_key}"' in text
    # They arrived by a grant, so the Source facet says so...
    assert 'data-origin="granted"' in text
    # ...and they are tagged as shared with the caller, not owned by them.
    assert 'data-ownership="shared_with_me"' in text


def test_granted_rows_link_to_the_individual_item(seeded_app):
    """A Library row is ONE item, so it opens that item's page — never the
    generic listing. Memory rows used to hand every domain the same
    /corporate-memory href, which lost which row was clicked; ?source=library
    additionally makes the drill-down's back link return here."""
    from src.repositories import data_packages_repo, memory_domains_repo

    gid = _group_with_member("analyst1", "lib-drilldown-grp")
    pkg = data_packages_repo().create(
        name="Drilldown Data",
        slug="lib-drill-data",
        description=None,
        icon=None,
        color=None,
        created_by="admin",
    )
    dom = memory_domains_repo().create(
        name="Drilldown Memory",
        slug="lib-drill-memory",
        description=None,
        icon=None,
        color=None,
        created_by="admin",
    )
    _grant(gid, "data_package", pkg)
    _grant(gid, "memory_domain", dom)

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "/catalog/p/lib-drill-data" in text
    assert "/memory/d/lib-drill-memory?source=library" in text


def test_granted_resources_are_not_reshareable_by_the_caller(seeded_app):
    """A granted resource is an admin's to share, not the recipient's — so its
    row carries no Share action (only the explain-sharing affordance)."""
    from src.repositories import data_packages_repo

    gid = _group_with_member("analyst1", "lib-noreshare-grp")
    pkg = data_packages_repo().create(
        name="Locked Package",
        slug="lib-locked",
        description="d",
        icon=None,
        color=None,
        created_by="admin",
    )
    _grant(gid, "data_package", pkg)

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Locked Package" in text
    # The data-package row is not owner-shareable.
    assert 'data-share-type="data_package"' not in text
    # And the sharing API refuses it outright (not a shareable type).
    r = seeded_app["client"].put(
        f"/api/sharing/data_package/{pkg}", json={"group_ids": []}, headers=_auth(seeded_app["analyst_token"])
    )
    assert r.status_code == 404


def test_library_excludes_resources_not_granted_to_me(seeded_app):
    """No grant → not in my Library (the page is access-scoped, not a catalogue
    of everything in the instance)."""
    from src.repositories import data_packages_repo

    data_packages_repo().create(
        name="Ungranted Package",
        slug="lib-ungranted",
        description="d",
        icon=None,
        color=None,
        created_by="admin",
    )
    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Ungranted Package" not in text
