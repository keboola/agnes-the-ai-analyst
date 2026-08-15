"""GET /catalog — unified Browse / My Stack card grid (Task 8.2 of v49 plan).

The page replaces the old per-source-card layout with marketplace.html
parity: hero + tab strip + filter chips + search + card grid using the
shared `_stack_card.html` macro. Per-table drill-down moves into
/catalog/p/<slug> (Task 8.3).

These tests render the page with seeded users + grants and assert the
new structure (tabs, chips, cards, empty banner) without asserting
on legacy markup.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _auto_membership_mode(monkeypatch):
    """This suite pins the AUTO-membership semantics, which are opt-in since
    the classic subscribe model became the default again (spec
    2026-08-07-default-chrome-ux-parity). Classic-mode contracts live in
    tests/test_stack_membership_modes.py."""
    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_pkg(slug: str = "sales-bundle", name: str = "Sales bundle"):
    """Create a data package and return its id."""
    from src.db import get_system_db
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    try:
        pkg_id = DataPackagesRepository(conn).create(
            name=name,
            slug=slug,
            description=f"{name} desc",
            icon="📦",
            color="#fce7f3",
            created_by="test",
        )
    finally:
        conn.close()
    return pkg_id


def _grant(
    group_name: str,
    resource_type: str,
    resource_id: str,
    requirement: str = "available",
    users: list[str] | None = None,
):
    """Add a resource_grants row for the named user-group.

    Also ensure ``users`` (typically the test's analyst id) are members of
    the group so the resolver picks up the grant — seeded_app puts only
    admin1 in the Admin group; everybody else has zero memberships by
    default in the test fixture.
    """
    import uuid
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()
        if not gid:
            return
        group_id = gid[0]
        if users:
            members = UserGroupMembersRepository(conn)
            for u in users:
                try:
                    members.add_member(u, group_id, source="test")
                except Exception:
                    pass
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id, resource_type, resource_id, requirement],
        )
    finally:
        conn.close()


class TestCatalogUnifiedPage:
    """NOTE (Wave 0 legacy retirement, 2026-08): despite the class/file name,
    every test here except the one below actually exercised the OLD
    ``catalog_legacy.html`` topnav template — "Browse"/"My Stack" tabs,
    "All"/"Required" filter chips, ``stack-grid``/``data-view="my"`` markup —
    not the kind-tabs page this file's own name suggests
    (``catalog_unified.html``, e.g. ``uc-kindtabs``/``uc-grid``, see
    ``TestRailOptIn::test_rail_catalog_renders_unified_page`` in
    ``test_ui_layout_theme.py``). Confirmed via ``git show`` against the
    template before its Wave-0 deletion: every one of those literals lived
    ONLY in ``catalog_legacy.html``. Task 4 deleted that template and
    collapsed ``/catalog`` onto the redesigned page unconditionally, so the
    four tests that pinned it are gone — one (``IndexError`` on a
    ``data-view="my"`` split that no longer exists) was outright broken by
    the deletion, the other three asserted CSS classes/attributes
    (``is-required``, ``data-action="add"``, the chip labels) that never
    existed in ``catalog_unified.html`` to begin with. The one test below
    still exercises the live page's real empty state."""

    def test_analyst_no_grants_sees_empty_state_banner(self, seeded_app):
        """Without any data_package grant, the analyst lands on the empty
        banner — no cards, explicit CTA."""
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        # Empty banner copy hints at the admin-grant path.
        assert "ask your admin" in body.lower() or "No data packages" in body


class TestRailClassicCatalogContracts:
    """Rail chrome (the only chrome since Wave 0, 2026-08) + CLASSIC
    membership, reached via the still-fully-supported explicit per-knob
    opt-out (auto-membership is what an unconfigured instance gets since the
    sole remaining `redesign` experience coupled to it): the unified page
    must apply ONE contract to both server-rendered kinds — the full granted
    set with add-to-stack state — not full-set Data next to addable-only
    Memory (Devin Review on #1199)."""

    @pytest.fixture(autouse=True)
    def _rail_classic(self, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "0")

    def test_granted_memory_domain_renders_on_the_memory_tab(self, seeded_app):
        import uuid

        from src.db import get_system_db
        from src.repositories.memory_domains import MemoryDomainsRepository

        conn = get_system_db()
        try:
            dom_id = MemoryDomainsRepository(conn).create(
                name="Classic Rail Domain",
                slug="classic-rail-dom",
                description="d",
                icon=None,
                color=None,
                created_by="test",
            )
            item_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO knowledge_items(id, title, content, status) VALUES (?, 'ki', 'body', 'approved')",
                [item_id],
            )
            MemoryDomainsRepository(conn).add_item(dom_id, item_id, added_by="test")
        finally:
            conn.close()
        _grant("Everyone", "memory_domain", dom_id, requirement="required", users=["analyst1"])

        body = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["analyst_token"])).text
        assert "Classic Rail Domain" in body, (
            "classic rail catalog must list granted memory domains (full granted set, same contract as the Data grid)"
        )

    def test_admin_sees_god_mode_on_both_kinds(self, seeded_app):
        """Classic restores admin god-mode Browse — and it must apply to BOTH
        server-rendered kinds on the unified page, or an admin sees god-mode
        Data next to grant-scoped Memory (Devin Review on #1199, round 2).
        Seed an UNGRANTED package and domain; the admin still sees both."""
        import uuid

        from src.db import get_system_db
        from src.repositories.memory_domains import MemoryDomainsRepository

        _make_pkg("ungranted-pkg", "Ungranted Package")
        conn = get_system_db()
        try:
            dom_id = MemoryDomainsRepository(conn).create(
                name="Ungranted Domain",
                slug="ungranted-dom",
                description="d",
                icon=None,
                color=None,
                created_by="test",
            )
            item_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO knowledge_items(id, title, content, status) VALUES (?, 'ki', 'body', 'approved')",
                [item_id],
            )
            MemoryDomainsRepository(conn).add_item(dom_id, item_id, added_by="test")
        finally:
            conn.close()

        body = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["admin_token"])).text
        assert "Ungranted Package" in body, "classic admin god-mode must cover the Data grid"
        assert "Ungranted Domain" in body, "classic admin god-mode must cover the Memory kind-tab too"

    def test_classic_card_actions_speak_membership_not_download(self, seeded_app):
        """Classic: the generic /api/stack endpoints JOIN/LEAVE the stack, so
        the unified cards must say Add-to-stack/Remove — download wording on
        a membership-changing control would cost a user their query access
        (Devin Review on #1199, round 5). Auto keeps the download toggle
        (pinned by the auto suites)."""
        pkg_id = _make_pkg("classic-wording", "Classic Wording Pkg")
        _grant("Everyone", "data_package", pkg_id, requirement="available", users=["analyst1"])
        body = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["analyst_token"])).text
        assert "Add to stack" in body
        assert "Download locally" not in body
        assert "Remove local copy" not in body

    def test_required_packages_render_before_available_ones_in_the_data_grid(self, seeded_app):
        """The Data grid groups Required cards first (first-demo feedback,
        2026-05-19: "bylo by dobre ty required mit vzdy nekde seskupene
        spolu na jedne strane") — required must cluster at the top instead
        of being scattered by creation order (``_req_first_key`` in the
        /catalog route, mirrored at /corporate-memory).

        Classic is what actually exercises this on /catalog: under
        auto-membership every granted package is already ``in_stack=True``,
        so none of them render in the Data grid at all — they live on My
        Stack (a separate route, /stack) instead, and /catalog's own
        (unused) ``stack_entries`` context var is never rendered by
        ``catalog_unified.html``. Classic keeps the full granted set in
        this grid, which is the only mode where the grid's ordering is
        observable at all.

        Seeded in deliberately-wrong alphabetical AND creation order so the
        sort has something real to undo: the required package's name
        ("ZZZ...") would sort LAST if name were the only key, proving the
        assertion pins the required-first grouping and not an accidental
        alphabetical coincidence.
        """
        avail_slug, req_slug, avail_slug_2 = "rail-classic-a-avail", "rail-classic-z-req", "rail-classic-m-avail"
        avail_pkg = _make_pkg(avail_slug, "AAA Available")
        req_pkg = _make_pkg(req_slug, "ZZZ Required")
        avail_pkg_2 = _make_pkg(avail_slug_2, "MMM Available")
        _grant("Everyone", "data_package", avail_pkg, requirement="available", users=["analyst1"])
        _grant("Everyone", "data_package", req_pkg, requirement="required", users=["analyst1"])
        _grant("Everyone", "data_package", avail_pkg_2, requirement="available", users=["analyst1"])

        body = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["analyst_token"])).text
        # Cards carry no generic `data-id` — each one's identity in the
        # rendered grid is its drilldown link (`c.href`, unique per slug).
        i_req = body.find(f'href="/catalog/p/{req_slug}"')
        i_a1 = body.find(f'href="/catalog/p/{avail_slug}"')
        i_a2 = body.find(f'href="/catalog/p/{avail_slug_2}"')
        assert i_req != -1 and i_a1 != -1 and i_a2 != -1, "all three cards must render in the Data grid"
        assert i_req < i_a1, f"Required card must render before available card 'AAA' (req@{i_req}, avail@{i_a1})"
        assert i_req < i_a2, f"Required card must render before available card 'MMM' (req@{i_req}, avail@{i_a2})"


class TestRailClassicLedeMatchesTheGrid:
    """The lede must describe what the tabs actually hold.

    Rail (the only chrome since Wave 0) + classic (reached via the explicit
    per-knob opt-out — auto-membership is the ambient default now that the
    sole remaining `redesign` experience is coupled to it). Its Data/Memory
    tabs list the FULL granted set — the one-contract-across-kinds decision
    pinned by `TestRailClassicCatalogContracts` — so the auto-membership copy
    "data and memory you've already been granted live in My Stack, not here"
    would have contradicted the grid directly below it (Devin on #1199).
    """

    def _lede(self, seeded_app):
        body = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["analyst_token"])).text
        start = body.index('<p class="lede">')
        return body[start : body.index("</p>", start)]

    def test_classic_lede_does_not_claim_granted_data_is_elsewhere(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "0")
        lede = self._lede(seeded_app)
        assert "not here" not in lede, "classic lists granted data on this page — the lede must not deny it"
        assert "not yet added" in lede

    def test_auto_membership_keeps_the_original_copy(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")
        lede = self._lede(seeded_app)
        assert "not here" in lede, "auto-membership copy changed — that surface is not what this PR touches"
