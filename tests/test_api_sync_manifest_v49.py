"""Tests for the v49 manifest extensions (Task 6.5).

Section 5.1 of the unified-stack design adds three top-level arrays to
``GET /api/sync/manifest``:

* ``data_packages``    — packages in the user's stack, with embedded tables
* ``memory_domains``   — memory domains in the user's stack
* ``direct_tables``    — tables granted directly (not via a package)

Plus the server-side telemetry event ``sync.pull_started``.
"""

from __future__ import annotations

import uuid

import pytest

from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _grant(group_id: str, resource_type: str, resource_id: str, requirement: str = "available") -> str:
    conn = get_system_db()
    grant_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'test')",
        [grant_id, group_id, resource_type, resource_id, requirement],
    )
    conn.close()
    return grant_id


def _create_group_with_analyst(name: str) -> str:
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    g = UserGroupsRepository(conn).create(name=name, description="", created_by="test")
    gid = g["id"] if isinstance(g, dict) else g
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.close()
    return gid


def _create_package(slug: str, name: str) -> str:
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    pkg_id = DataPackagesRepository(conn).create(
        name=name, slug=slug, description=None,
        icon="📦", color="#abc", created_by="test",
    )
    conn.close()
    return pkg_id


def _create_memory_domain(slug: str, name: str) -> str:
    from src.repositories.memory_domains import MemoryDomainsRepository

    conn = get_system_db()
    did = MemoryDomainsRepository(conn).create(
        name=name, slug=slug, description=None,
        icon="🎯", color="#dcfce7", created_by="test",
    )
    conn.close()
    return did


def _register_table(name: str) -> str:
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    table_id = f"tbl_{name}"
    TableRegistryRepository(conn).register(
        id=table_id,
        name=name,
        source_type="keboola",
        source_table=f"in.c-test.{name}",
        bucket="in.c-test",
        query_mode="local",
    )
    conn.close()
    return table_id


def _telemetry_count(event_type: str, user_id: str) -> int:
    conn = get_system_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM usage_events "
        "WHERE event_type = ? AND user_id = ? AND source = 'server'",
        [event_type, user_id],
    ).fetchone()[0]
    conn.close()
    return n


class TestManifestExtensions:
    def test_manifest_has_v49_arrays(self, seeded_app):
        """Even with no grants the new top-level fields are present so older
        manifest readers don't NPE on a missing key."""
        resp = seeded_app["client"].get(
            "/api/sync/manifest",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "data_packages" in body
        assert "memory_domains" in body
        assert "direct_tables" in body
        assert "tables" in body  # legacy paralel

    def test_manifest_contains_subscribed_data_package(self, seeded_app):
        gid = _create_group_with_analyst("MPkg")
        pkg_id = _create_package("manifest-pkg", "ManifestPkg")
        _grant(gid, "data_package", pkg_id, "available")
        # subscribe
        seeded_app["client"].post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = seeded_app["client"].get(
            "/api/sync/manifest",
            headers=_auth(seeded_app["analyst_token"]),
        ).json()
        slugs = [p["slug"] for p in body["data_packages"]]
        assert "manifest-pkg" in slugs

    def test_manifest_contains_required_memory_domain(self, seeded_app):
        gid = _create_group_with_analyst("MDom")
        did = _create_memory_domain("manifest-dom", "ManifestDom")
        _grant(gid, "memory_domain", did, "required")
        body = seeded_app["client"].get(
            "/api/sync/manifest",
            headers=_auth(seeded_app["analyst_token"]),
        ).json()
        slugs = [d["slug"] for d in body["memory_domains"]]
        assert "manifest-dom" in slugs
        dom = next(d for d in body["memory_domains"] if d["slug"] == "manifest-dom")
        assert dom["requirement"] == "required"
        assert "md5" in dom and "bundle_url" in dom

    def test_manifest_emits_sync_pull_started(self, seeded_app):
        before = _telemetry_count("sync.pull_started", "analyst1")
        seeded_app["client"].get(
            "/api/sync/manifest",
            headers=_auth(seeded_app["analyst_token"]),
        )
        after = _telemetry_count("sync.pull_started", "analyst1")
        assert after == before + 1

    def test_manifest_direct_tables_dedupes_packaged(self, seeded_app):
        """A table granted both directly and via a package shouldn't appear
        twice — it stays under the package and is filtered out of
        direct_tables."""
        gid = _create_group_with_analyst("MDir")
        pkg_id = _create_package("dedupe-pkg", "DedupePkg")
        table_id = _register_table("orders_manifest")
        # Attach table to package
        from src.repositories.data_packages import DataPackagesRepository
        conn = get_system_db()
        DataPackagesRepository(conn).add_table(pkg_id, table_id, added_by="test")
        conn.close()
        _grant(gid, "data_package", pkg_id, "required")  # required → in stack auto
        _grant(gid, "table", table_id, "available")
        body = seeded_app["client"].get(
            "/api/sync/manifest",
            headers=_auth(seeded_app["analyst_token"]),
        ).json()
        direct_ids = [t["id"] for t in body["direct_tables"]]
        assert table_id not in direct_ids
        # But it still appears under the package
        pkg = next(p for p in body["data_packages"] if p["slug"] == "dedupe-pkg")
        pkg_table_ids = [t["id"] for t in pkg["tables"]]
        assert table_id in pkg_table_ids
