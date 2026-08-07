"""Catalog reshape — follow-up to the auto-membership change.

Every RBAC-granted Data Package / Memory Domain is automatically part of
the caller's stack (auto-membership: ``StackResolver.browse()`` sets
``in_stack=True`` unconditionally on every entry it returns). This means
the Catalog's whole purpose flips: it's no longer "everything you have
access to" (that's My Stack) but "things you can still ADD" — genuinely
addable, not-already-in-stack resources (fleamarket entities, curated
marketplace plugins). Two behavior changes pinned here:

1. ``/catalog`` and ``/corporate-memory`` no longer god-mode admins via
   ``StackResolver.browse_admin`` — admin and non-admin see the same
   grant-scoped, addable-only Data/Memory grid.
2. The old "see every package regardless of grant" admin audit moved to
   ``/admin/data-packages``.
"""

from __future__ import annotations

import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_pkg(slug: str, name: str) -> str:
    from src.db import get_system_db
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    try:
        return DataPackagesRepository(conn).create(
            name=name,
            slug=slug,
            description=f"{name} desc",
            icon=None,
            color=None,
            created_by="test",
        )
    finally:
        conn.close()


def _grant(
    group_name: str,
    resource_type: str,
    resource_id: str,
    *,
    requirement: str = "available",
    users: list[str] | None = None,
):
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        gid_row = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()
        if not gid_row:
            return
        group_id = gid_row[0]
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


class TestCatalogExcludesInStackItems:
    def test_analyst_granted_package_absent_from_data_grid(self, seeded_app):
        """A package granted to the analyst's group is auto-membership
        in_stack=True — it must not render in the Catalog's Data grid
        (classic layout's Browse tab)."""
        pkg_id = _make_pkg("reshape-avail", "Reshape Available Pkg")
        _grant("Everyone", "data_package", pkg_id, requirement="available", users=["analyst1"])

        c = seeded_app["client"]
        resp = c.get("/catalog", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        body = resp.text
        browse_section = body.split('data-view="browse"', 1)[1].split('data-view="my"', 1)[0]
        assert pkg_id not in browse_section, "granted (in-stack) package must not appear in the Browse/Data grid"
        # It's still visible somewhere — the My Stack tab.
        assert pkg_id in body

    def test_analyst_required_package_absent_from_data_grid(self, seeded_app):
        """Required packages are also always in_stack=True — same
        exclusion applies, even though they're mandatory."""
        pkg_id = _make_pkg("reshape-required", "Reshape Required Pkg")
        _grant("Everyone", "data_package", pkg_id, requirement="required", users=["analyst1"])

        c = seeded_app["client"]
        resp = c.get("/catalog", headers=_auth(seeded_app["analyst_token"]))
        body = resp.text
        browse_section = body.split('data-view="browse"', 1)[1].split('data-view="my"', 1)[0]
        assert pkg_id not in browse_section
        assert pkg_id in body

    def test_admin_no_longer_sees_ungranted_package_on_catalog(self, seeded_app):
        """Admin god-mode (``browse_admin``) is removed from the
        user-facing /catalog — an admin with no grant to a package must
        not see it there at all (moved to /admin/data-packages)."""
        pkg_id = _make_pkg("reshape-admin-ungranted", "Admin Ungranted Pkg")
        # No grant at all.
        c = seeded_app["client"]
        resp = c.get("/catalog", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert pkg_id not in resp.text

    def test_analyst_no_grants_sees_empty_state_pointing_to_my_stack(self, seeded_app):
        """Empty-state copy differentiates "nothing exists/granted" from
        "you already have everything" — analyst here has zero grants at
        all, so gets the "ask your admin" copy, not the "already have
        everything" copy."""
        c = seeded_app["client"]
        resp = c.get("/catalog", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "ask your admin" in resp.text.lower()


class TestMemoryCatalogExcludesInStackItems:
    def test_analyst_granted_domain_absent_from_browse_grid(self, seeded_app):
        from src.db import get_system_db
        from src.repositories.memory_domains import MemoryDomainsRepository
        from src.repositories.knowledge import KnowledgeRepository

        conn = get_system_db()
        try:
            dom_id = MemoryDomainsRepository(conn).create(
                slug="reshape-mem",
                name="Reshape Memory",
                description="d",
                icon="🎯",
                color="#dcfce7",
                created_by="test",
            )
            item_id = str(uuid.uuid4())
            kr = KnowledgeRepository(conn)
            kr.create(
                id=item_id,
                title="starter",
                content="seeded",
                category="convention",
                domain="reshape-mem",
                source_type="manual",
                source_user="test",
            )
            kr.update_status(item_id, "approved")
        finally:
            conn.close()
        _grant("Everyone", "memory_domain", dom_id, requirement="available", users=["analyst1"])

        c = seeded_app["client"]
        resp = c.get("/corporate-memory", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        body = resp.text
        browse_section = body.split('data-view="browse"', 1)[1].split('data-view="my"', 1)[0]
        assert "Reshape Memory" not in browse_section
        assert "Reshape Memory" in body

    def test_admin_no_longer_god_mode_on_corporate_memory(self, seeded_app):
        """An admin with no grant to a domain must not see it via the old
        browse_admin path — /corporate-memory now runs the same
        grant-scoped browse() as everyone."""
        from src.db import get_system_db
        from src.repositories.memory_domains import MemoryDomainsRepository

        conn = get_system_db()
        try:
            MemoryDomainsRepository(conn).create(
                slug="reshape-admin-mem",
                name="Admin Ungranted Memory",
                description="d",
                icon="🎯",
                color="#dcfce7",
                created_by="test",
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        resp = c.get("/corporate-memory", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Admin Ungranted Memory" not in resp.text


class TestAdminDataPackagesAuditPage:
    def test_admin_sees_all_packages_regardless_of_grant(self, seeded_app):
        pkg_id = _make_pkg("audit-ungranted", "Audit Ungranted Pkg")
        # No grant at all — browse_admin still surfaces it for the audit page.
        c = seeded_app["client"]
        resp = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Audit Ungranted Pkg" in resp.text
        assert pkg_id in resp.text

    def test_admin_sees_all_memory_domains_regardless_of_grant(self, seeded_app):
        from src.db import get_system_db
        from src.repositories.memory_domains import MemoryDomainsRepository

        conn = get_system_db()
        try:
            MemoryDomainsRepository(conn).create(
                slug="audit-ungranted-mem",
                name="Audit Ungranted Memory",
                description="d",
                icon="🎯",
                color="#dcfce7",
                created_by="test",
            )
        finally:
            conn.close()

        c = seeded_app["client"]
        resp = c.get("/admin/data-packages", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Audit Ungranted Memory" in resp.text

    def test_non_admin_forbidden(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/data-packages", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code in (401, 403)

    def test_admin_hub_links_to_audit_page(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "/admin/data-packages" in resp.text
