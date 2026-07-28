"""Tests for /api/data-packages/{slug} + /api/memory/domains/{slug} (Task 6.6).

Covers RBAC (admin bypass, grant required for non-admin), 404 on unknown
slug, and telemetry emission (data_package.view / memory_domain.view).
"""

from __future__ import annotations

import json
import uuid

import pytest

from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _grant(group_id: str, resource_type: str, resource_id: str) -> None:
    conn = get_system_db()
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, ?, ?, 'available', CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), group_id, resource_type, resource_id],
    )
    conn.close()


def _create_group_with_analyst(name: str) -> str:
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    g = UserGroupsRepository(conn).create(name=name, description="", created_by="test")
    gid = g["id"] if isinstance(g, dict) else g
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.close()
    return gid


def _create_package(slug: str = "view-pkg") -> str:
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    pkg_id = DataPackagesRepository(conn).create(
        name="ViewPkg", slug=slug, description="d",
        icon="📦", color="#abc", created_by="test",
    )
    conn.close()
    return pkg_id


def _create_domain(slug: str = "view-dom") -> str:
    from src.repositories.memory_domains import MemoryDomainsRepository

    conn = get_system_db()
    did = MemoryDomainsRepository(conn).create(
        name="ViewDom", slug=slug, description="d",
        icon="🎯", color="#dcfce7", created_by="test",
    )
    conn.close()
    return did


def _telemetry_count(event_type: str, user_id: str, slug: str) -> int:
    conn = get_system_db()
    rows = conn.execute(
        "SELECT friction_tags FROM usage_events "
        "WHERE event_type = ? AND user_id = ? AND source = 'server'",
        [event_type, user_id],
    ).fetchall()
    conn.close()
    n = 0
    for r in rows:
        if r[0]:
            try:
                if json.loads(r[0]).get("slug") == slug:
                    n += 1
            except Exception:
                pass
    return n


class TestDataPackageView:
    def test_admin_can_view_any(self, seeded_app):
        _create_package("admin-view")
        resp = seeded_app["client"].get(
            "/api/data-packages/admin-view",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["slug"] == "admin-view"

    def test_non_admin_without_grant_403(self, seeded_app):
        _create_package("forbidden")
        resp = seeded_app["client"].get(
            "/api/data-packages/forbidden",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_non_admin_with_grant_sees_detail_and_emits_view(self, seeded_app):
        gid = _create_group_with_analyst("ViewG")
        pkg_id = _create_package("granted-pkg")
        _grant(gid, "data_package", pkg_id)
        before = _telemetry_count("data_package.view", "analyst1", "granted-pkg")
        resp = seeded_app["client"].get(
            "/api/data-packages/granted-pkg?source=browse",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        after = _telemetry_count("data_package.view", "analyst1", "granted-pkg")
        assert after == before + 1

    def test_unknown_slug_404(self, seeded_app):
        resp = seeded_app["client"].get(
            "/api/data-packages/does-not-exist",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404


class TestMemoryDomainView:
    def test_admin_can_view_any(self, seeded_app):
        _create_domain("admin-dom")
        resp = seeded_app["client"].get(
            "/api/memory/domains/admin-dom",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["slug"] == "admin-dom"

    def test_non_admin_without_grant_403(self, seeded_app):
        _create_domain("priv-dom")
        resp = seeded_app["client"].get(
            "/api/memory/domains/priv-dom",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_non_admin_with_grant_sees_detail_and_emits_view(self, seeded_app):
        gid = _create_group_with_analyst("ViewDomG")
        did = _create_domain("granted-dom")
        _grant(gid, "memory_domain", did)
        before = _telemetry_count("memory_domain.view", "analyst1", "granted-dom")
        resp = seeded_app["client"].get(
            "/api/memory/domains/granted-dom?source=my-stack",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        after = _telemetry_count("memory_domain.view", "analyst1", "granted-dom")
        assert after == before + 1

    def test_unknown_slug_404(self, seeded_app):
        resp = seeded_app["client"].get(
            "/api/memory/domains/nope",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404
