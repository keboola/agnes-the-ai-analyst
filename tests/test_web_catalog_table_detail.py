"""GET /catalog/t/<table_id> — per-table drill-down (FAI-132 t6).

Closes a coverage gap: prior to this test the route had no behavioral
test, despite being the worst N+1 offender in ``app/web/router.py``
(a per-package ``list_tables`` call inside a ``for p in pkg_repo.list()``
loop). Pins the refactor to ``pkg_repo.list_member_ids_bulk()`` +
``get_accessible_ids(user, DATA_PACKAGE, conn)``: admin god-mode, grant
on a parent package, and 403 for an ungranted analyst.
"""

from __future__ import annotations

import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_table(table_id: str, name: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=table_id,
            name=name,
            source_type="keboola",
            bucket="in.c-test",
            source_table=table_id,
            query_mode="local",
        )
    finally:
        conn.close()


def _make_pkg(slug: str, name: str) -> str:
    from src.db import get_system_db
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    try:
        return DataPackagesRepository(conn).create(
            name=name,
            slug=slug,
            description=f"{name} desc",
            icon="📦",
            color="#fce7f3",
            created_by="test",
        )
    finally:
        conn.close()


def _add_table_to_pkg(pkg_id: str, table_id: str) -> None:
    from src.db import get_system_db
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    try:
        DataPackagesRepository(conn).add_table(pkg_id, table_id, added_by="test")
    finally:
        conn.close()


def _grant_pkg(group_name: str, resource_id: str, requirement: str = "available", users: list[str] | None = None):
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        gid_row = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()
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


class TestCatalogTableDetail:
    def test_unknown_table_returns_404(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/catalog/t/does-not-exist", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 404

    def test_analyst_without_grant_blocked(self, seeded_app):
        table_id = f"t-{uuid.uuid4().hex[:8]}"
        _make_table(table_id, "Locked Table")
        pkg_id = _make_pkg(f"locked-pkg-{uuid.uuid4().hex[:8]}", "Locked Pkg")
        _add_table_to_pkg(pkg_id, table_id)
        c = seeded_app["client"]
        resp = c.get(f"/catalog/t/{table_id}", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_analyst_with_grant_on_parent_package_sees_table(self, seeded_app):
        table_id = f"t-{uuid.uuid4().hex[:8]}"
        _make_table(table_id, "Granted Table")
        pkg_id = _make_pkg(f"granted-pkg-{uuid.uuid4().hex[:8]}", "Granted Pkg")
        _add_table_to_pkg(pkg_id, table_id)
        _grant_pkg("Everyone", pkg_id, requirement="available", users=["analyst1"])
        c = seeded_app["client"]
        resp = c.get(f"/catalog/t/{table_id}", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "Granted Table" in resp.text
        assert "Granted Pkg" in resp.text

    def test_admin_sees_table_even_without_parent_package(self, seeded_app):
        table_id = f"t-{uuid.uuid4().hex[:8]}"
        _make_table(table_id, "Orphan Table")
        c = seeded_app["client"]
        resp = c.get(f"/catalog/t/{table_id}", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Orphan Table" in resp.text

    def test_admin_sees_correct_parent_packages(self, seeded_app):
        table_id = f"t-{uuid.uuid4().hex[:8]}"
        _make_table(table_id, "Multi Pkg Table")
        pkg1 = _make_pkg(f"pkg-a-{uuid.uuid4().hex[:8]}", "Pkg Alpha")
        pkg2 = _make_pkg(f"pkg-b-{uuid.uuid4().hex[:8]}", "Pkg Beta")
        _add_table_to_pkg(pkg1, table_id)
        _add_table_to_pkg(pkg2, table_id)
        c = seeded_app["client"]
        resp = c.get(f"/catalog/t/{table_id}", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Pkg Alpha" in resp.text
        assert "Pkg Beta" in resp.text
