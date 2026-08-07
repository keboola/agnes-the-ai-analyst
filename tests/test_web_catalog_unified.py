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
    def test_admin_sees_hero_and_tabs(self, seeded_app):
        """Hero + tab strip (Browse / My Stack) + filter chips + grid container
        all render for admin — same grant-scoped view as any other user
        (god-mode Browse was removed; see /admin/data-packages for the
        full-audit view)."""
        _make_pkg("admin-test-pkg-1", "Sales bundle")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/catalog", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        # Hero + tab strip mirrors marketplace.html structure.
        assert "Data Packages" in body
        # Browse / My Stack tabs.
        assert "Browse" in body
        assert "My Stack" in body
        # Filter chips.
        assert "All" in body
        assert "Required" in body
        # Grid container present (rendered even if empty).
        assert "stack-grid" in body or "stack-empty" in body

    def test_analyst_with_required_grant_sees_package_card(self, seeded_app):
        """Required grant for the analyst's Everyone group surfaces the
        package on Browse with the Required badge."""
        pkg_id = _make_pkg("eng-bundle", "Engineering bundle")
        _grant("Everyone", "data_package", pkg_id, requirement="required", users=["analyst1"])
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Engineering bundle" in body
        # Required state.
        assert "is-required" in body

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

    def test_card_buttons_carry_data_action_attrs(self, seeded_app):
        """JS wiring for Add/Remove rides on data-action attributes."""
        pkg_id = _make_pkg("avail-pkg", "Available pkg")
        _grant("Everyone", "data_package", pkg_id, requirement="available", users=["analyst1"])
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        # Available + not subscribed → Add button with data-action="add".
        assert 'data-action="add"' in body

    def test_required_packages_render_before_available_ones(self, seeded_app):
        """My Stack groups Required cards first (first-demo feedback).

        Three packages: two available + one required. The required card
        must come BEFORE the available ones in the rendered HTML so it
        clusters at the top of the grid instead of being interleaved by
        creation order.

        Catalog reshape note: every granted package (required or
        available) is auto-membership in_stack=True, so none of these
        three render in the Browse/Data grid anymore — they all render
        under My Stack instead, which is why the ordering assertion now
        targets that grid (the sort itself moved with the content, see
        /catalog route's ``_req_first_key``).
        """
        # Seed in deliberately-wrong order (available first) so the sort
        # has something to undo.
        avail_pkg = _make_pkg("a-avail", "AAA Available")
        req_pkg = _make_pkg("z-req", "ZZZ Required")
        avail_pkg_2 = _make_pkg("m-avail", "MMM Available")
        _grant("Everyone", "data_package", avail_pkg, requirement="available", users=["analyst1"])
        _grant("Everyone", "data_package", req_pkg, requirement="required", users=["analyst1"])
        _grant("Everyone", "data_package", avail_pkg_2, requirement="available", users=["analyst1"])

        resp = seeded_app["client"].get(
            "/catalog",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = resp.text
        my_stack_section = body.split('data-view="my"', 1)[1]
        # The required-grant card must appear earlier in the document
        # than either available card — independent of creation order or
        # alphabetical name ordering.
        i_req = my_stack_section.find('data-id="' + req_pkg + '"')
        i_a1 = my_stack_section.find('data-id="' + avail_pkg + '"')
        i_a2 = my_stack_section.find('data-id="' + avail_pkg_2 + '"')
        assert i_req != -1 and i_a1 != -1 and i_a2 != -1
        assert i_req < i_a1, f"Required card must render before available card 'AAA' (req@{i_req}, avail@{i_a1})"
        assert i_req < i_a2, f"Required card must render before available card 'MMM' (req@{i_req}, avail@{i_a2})"


class TestRailClassicCatalogContracts:
    """Rail chrome + CLASSIC membership (the combination every redesign
    adopter lands in until they enable the stack flag): the unified page must
    apply ONE contract to both server-rendered kinds — the full granted set
    with add-to-stack state — not full-set Data next to addable-only Memory
    (Devin Review on #1199)."""

    @pytest.fixture(autouse=True)
    def _rail_classic(self, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        monkeypatch.delenv("AGNES_STACK_AUTO_MEMBERSHIP", raising=False)

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


class TestRailClassicLedeMatchesTheGrid:
    """The lede must describe what the tabs actually hold.

    Rail + classic is reachable per-knob (`ui_layout: rail` without the
    redesign preset) and has no prior art. Its Data/Memory tabs list the FULL
    granted set — the one-contract-across-kinds decision pinned by
    `TestRailClassicCatalogContracts` — so the auto-membership copy "data and
    memory you've already been granted live in My Stack, not here" would have
    contradicted the grid directly below it (Devin on #1199).
    """

    def _lede(self, seeded_app):
        body = seeded_app["client"].get("/catalog", headers=_auth(seeded_app["analyst_token"])).text
        start = body.index('<p class="lede">')
        return body[start : body.index("</p>", start)]

    def test_classic_lede_does_not_claim_granted_data_is_elsewhere(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        monkeypatch.delenv("AGNES_STACK_AUTO_MEMBERSHIP", raising=False)
        lede = self._lede(seeded_app)
        assert "not here" not in lede, "classic lists granted data on this page — the lede must not deny it"
        assert "not yet added" in lede

    def test_auto_membership_keeps_the_original_copy(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")
        lede = self._lede(seeded_app)
        assert "not here" in lede, "auto-membership copy changed — that surface is not what this PR touches"
