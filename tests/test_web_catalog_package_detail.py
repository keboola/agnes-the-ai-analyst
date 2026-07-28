"""GET /catalog/p/<slug> — per-package drill-down (Task 8.3 of v49 plan).

Renders the package header (icon + name + description + Add/Remove) and
the per-table rows that used to live on the old /catalog source-card
layout. RBAC is enforced server-side: the route returns 403 for users
without a grant on the package, mirroring the API equivalent.

The page emits a ``data_package.view`` telemetry event via the API path
when the user resolves the data via the JS / browser navigation — the
template intentionally drives the data load through the same
``/api/data-packages/{slug}`` endpoint that has the audit-log emit so
behavior is identical between API and web rendering.
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
            name=name, slug=slug, description=f"{name} desc",
            icon="📦", color="#fce7f3", created_by="test",
        )
    finally:
        conn.close()


def _grant_pkg(group_name: str, resource_id: str, requirement: str = "available",
               users: list[str] | None = None):
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        gid_row = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [group_name]
        ).fetchone()
        if not gid_row:
            return
        group_id = gid_row[0]
        if users:
            for u in users:
                try:
                    UserGroupMembersRepository(conn).add_member(u, group_id, source="test")
                except Exception:
                    pass
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'data_package', ?, ?, CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id, resource_id, requirement],
        )
    finally:
        conn.close()


class TestCatalogPackageDetail:
    def test_unknown_slug_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/catalog/p/does-not-exist", headers=_auth(token))
        assert resp.status_code == 404

    def test_admin_can_view_any_package(self, seeded_app):
        """Admin god-mode short-circuits the grant check."""
        _make_pkg("admin-only-pkg", "Admin only pkg")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/catalog/p/admin-only-pkg", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Admin only pkg" in body
        # Back link to /catalog.
        assert 'href="/catalog"' in body

    def test_analyst_without_grant_blocked(self, seeded_app):
        _make_pkg("locked-pkg", "Locked pkg")
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/p/locked-pkg", headers=_auth(token))
        assert resp.status_code == 403

    def test_analyst_with_grant_sees_header_and_back_link(self, seeded_app):
        pid = _make_pkg("granted-pkg", "Granted pkg")
        _grant_pkg("Everyone", pid, requirement="available",
                   users=["analyst1"])
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/catalog/p/granted-pkg", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Granted pkg" in body
        assert "Granted pkg desc" in body
        assert 'href="/catalog"' in body
