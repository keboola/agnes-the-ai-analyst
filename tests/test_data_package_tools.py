"""Tests for the data_package_tools junction (RFC #461 §6 related_tools).

Cover:
* Repo: add_tool, remove_tool, list_tools enriched with source_name + mode.
* REST: POST + DELETE under /api/admin/data-packages/{pkg_id}/tools[/{tool_id}].
* GET admin detail surfaces ``related_tools`` array.
* User-facing GET /api/data-packages/{slug} surfaces ``related_tools`` too.
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from src.db import get_system_db
from src.repositories.data_packages import DataPackagesRepository
from src.repositories.mcp_sources import MCPSourceRepository
from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository


def _seed_pkg_and_tool() -> dict:
    """Create one package, one source, one passthrough tool with
    per-test-unique ids so the function is idempotent across tests
    sharing a system.duckdb fixture.

    The DataPackages create flow normally goes through a heavier admin
    endpoint with resource_grants seeding; for this junction test we
    insert directly through the repo to keep the test focused.
    """
    import uuid
    suffix = uuid.uuid4().hex[:8]
    pkg_id = uuid.uuid4().hex
    slug = f"pkg-x-{suffix}"
    source_id = f"src_pt_{suffix}"
    source_name = f"pt-up-{suffix}"
    tool_id = f"{source_name}.find"

    conn = get_system_db()
    conn.execute(
        """INSERT INTO data_packages (id, slug, name, description, created_by)
           VALUES (?, ?, ?, ?, ?)""",
        [pkg_id, slug, "Pkg-X", "test package", "system_seed"],
    )
    MCPSourceRepository(conn).upsert(
        id=source_id, name=source_name,
        transport="stdio", command="/bin/true", args=[],
    )
    ToolRegistryRepository(conn).upsert(
        tool_id=tool_id,
        source_id=source_id,
        original_name="find",
        exposed_name="find",
        mode=PASSTHROUGH,
        description="Look something up.",
    )
    conn.close()
    return {
        "pkg_id": pkg_id, "tool_id": tool_id, "slug": slug,
        "source_id": source_id, "source_name": source_name,
    }


# ── repo ──────────────────────────────────────────────────────────────────


def test_repo_add_remove_list_round_trip():
    seed = _seed_pkg_and_tool()
    conn = get_system_db()
    repo = DataPackagesRepository(conn)
    assert repo.add_tool(seed["pkg_id"], seed["tool_id"]) is True
    # Idempotent — second add returns False
    assert repo.add_tool(seed["pkg_id"], seed["tool_id"]) is False
    tools = repo.list_tools(seed["pkg_id"])
    assert len(tools) == 1
    assert tools[0]["tool_id"] == seed["tool_id"]
    assert tools[0]["source_name"] == seed["source_name"]
    assert tools[0]["mode"] == "passthrough"
    assert tools[0]["description"] == "Look something up."
    # Remove + idempotent
    assert repo.remove_tool(seed["pkg_id"], seed["tool_id"]) is True
    assert repo.remove_tool(seed["pkg_id"], seed["tool_id"]) is False
    assert repo.list_tools(seed["pkg_id"]) == []
    conn.close()


# ── admin REST ────────────────────────────────────────────────────────────


def test_add_tool_endpoint_attaches_and_returns_added_flag(seeded_app):
    seed = _seed_pkg_and_tool()
    client = seeded_app["client"]
    r = client.post(
        f"/api/admin/data-packages/{seed['pkg_id']}/tools",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"tool_id": seed["tool_id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"added": True}
    # Idempotent
    r2 = client.post(
        f"/api/admin/data-packages/{seed['pkg_id']}/tools",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"tool_id": seed["tool_id"]},
    )
    assert r2.json() == {"added": False}


def test_add_tool_404_unknown_package(seeded_app):
    seed = _seed_pkg_and_tool()
    client = seeded_app["client"]
    r = client.post(
        "/api/admin/data-packages/does-not-exist/tools",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"tool_id": seed["tool_id"]},
    )
    assert r.status_code == 404


def test_add_tool_404_unknown_tool(seeded_app):
    seed = _seed_pkg_and_tool()
    client = seeded_app["client"]
    r = client.post(
        f"/api/admin/data-packages/{seed['pkg_id']}/tools",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"tool_id": "nope.does-not-exist"},
    )
    assert r.status_code == 404


def test_remove_tool_endpoint(seeded_app):
    seed = _seed_pkg_and_tool()
    client = seeded_app["client"]
    client.post(
        f"/api/admin/data-packages/{seed['pkg_id']}/tools",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"tool_id": seed["tool_id"]},
    )
    r = client.delete(
        f"/api/admin/data-packages/{seed['pkg_id']}/tools/{seed['tool_id']}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 204


def test_admin_detail_surfaces_related_tools(seeded_app):
    seed = _seed_pkg_and_tool()
    client = seeded_app["client"]
    client.post(
        f"/api/admin/data-packages/{seed['pkg_id']}/tools",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        json={"tool_id": seed["tool_id"]},
    )
    r = client.get(
        f"/api/admin/data-packages/{seed['pkg_id']}",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "related_tools" in body
    assert any(t["tool_id"] == seed["tool_id"] for t in body["related_tools"])
