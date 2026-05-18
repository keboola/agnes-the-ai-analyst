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


def _grant(group_name: str, resource_type: str, resource_id: str,
           requirement: str = "available", users: list[str] | None = None):
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
        gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [group_name]
        ).fetchone()
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
        all render for admin (who sees every package via god-mode)."""
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
        _grant("Everyone", "data_package", pkg_id, requirement="required",
               users=["analyst1"])
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
        _grant("Everyone", "data_package", pkg_id, requirement="available",
               users=["analyst1"])
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        # Available + not subscribed → Add button with data-action="add".
        assert 'data-action="add"' in body
