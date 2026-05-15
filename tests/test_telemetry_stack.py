"""Telemetry coverage for every v49 user-side event (Task 6.7.4).

Maps to Section 9.2 of the unified-stack design — every event_type listed
there must produce at least one row in ``usage_events`` (source='server').
"""

from __future__ import annotations

import json
import uuid

import pytest

from src.db import get_system_db
from src.repositories.knowledge import KnowledgeRepository


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _events(event_type: str, user_id: str = "analyst1") -> list[dict]:
    conn = get_system_db()
    rows = conn.execute(
        "SELECT event_type, friction_tags FROM usage_events "
        "WHERE event_type = ? AND user_id = ? AND source = 'server' "
        "ORDER BY occurred_at",
        [event_type, user_id],
    ).fetchall()
    conn.close()
    return [
        {"event_type": e, "props": json.loads(p) if p else None}
        for e, p in rows
    ]


def _grant(group_id: str, resource_type: str, resource_id: str, requirement: str = "available") -> None:
    conn = get_system_db()
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), group_id, resource_type, resource_id, requirement],
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


def _create_package(slug: str) -> str:
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    pkg_id = DataPackagesRepository(conn).create(
        name="P", slug=slug, description=None, icon=None, color=None, created_by="test",
    )
    conn.close()
    return pkg_id


def _create_domain(slug: str) -> str:
    from src.repositories.memory_domains import MemoryDomainsRepository

    conn = get_system_db()
    did = MemoryDomainsRepository(conn).create(
        name="D", slug=slug, description=None, icon=None, color=None, created_by="test",
    )
    conn.close()
    return did


def _create_knowledge_item(is_required: bool = False) -> str:
    conn = get_system_db()
    item_id = "ki_" + uuid.uuid4().hex[:8]
    KnowledgeRepository(conn).create(
        id=item_id, title="T", content="x", category="engineering",
        status="approved", is_required=is_required,
    )
    conn.close()
    return item_id


# -------- stack.subscribe / stack.unsubscribe -----------------------------


def test_stack_subscribe_telemetry(seeded_app):
    gid = _create_group_with_analyst("TSub")
    pkg_id = _create_package("tsub-pkg")
    _grant(gid, "data_package", pkg_id, "available")
    before = len(_events("stack.subscribe"))
    seeded_app["client"].post(
        "/api/stack/subscribe",
        json={"resource_type": "data_package", "resource_id": pkg_id},
        headers=_auth(seeded_app["analyst_token"]),
    )
    after = _events("stack.subscribe")
    assert len(after) == before + 1
    assert after[-1]["props"]["resource_type"] == "data_package"
    assert after[-1]["props"]["resource_id"] == pkg_id


def test_stack_unsubscribe_telemetry(seeded_app):
    gid = _create_group_with_analyst("TUnsub")
    pkg_id = _create_package("tunsub-pkg")
    _grant(gid, "data_package", pkg_id, "available")
    c = seeded_app["client"]
    h = _auth(seeded_app["analyst_token"])
    c.post(
        "/api/stack/subscribe",
        json={"resource_type": "data_package", "resource_id": pkg_id},
        headers=h,
    )
    before = len(_events("stack.unsubscribe"))
    c.delete(f"/api/stack/subscription/data_package/{pkg_id}", headers=h)
    after = _events("stack.unsubscribe")
    assert len(after) == before + 1


# -------- memory.dismiss / memory.undismiss -------------------------------


def test_memory_dismiss_telemetry(seeded_app):
    item_id = _create_knowledge_item()
    before = len(_events("memory.dismiss"))
    seeded_app["client"].post(
        f"/api/memory/{item_id}/dismiss",
        headers=_auth(seeded_app["analyst_token"]),
    )
    after = _events("memory.dismiss")
    assert len(after) == before + 1
    assert after[-1]["props"]["item_id"] == item_id


def test_memory_undismiss_telemetry(seeded_app):
    item_id = _create_knowledge_item()
    c = seeded_app["client"]
    h = _auth(seeded_app["analyst_token"])
    c.post(f"/api/memory/{item_id}/dismiss", headers=h)
    before = len(_events("memory.undismiss"))
    c.delete(f"/api/memory/{item_id}/dismiss", headers=h)
    after = _events("memory.undismiss")
    assert len(after) == before + 1


# -------- data_package.view / memory_domain.view --------------------------


def test_data_package_view_telemetry(seeded_app):
    gid = _create_group_with_analyst("TView")
    pkg_id = _create_package("tview-pkg")
    _grant(gid, "data_package", pkg_id, "available")
    before = len(_events("data_package.view"))
    seeded_app["client"].get(
        "/api/data-packages/tview-pkg",
        headers=_auth(seeded_app["analyst_token"]),
    )
    after = _events("data_package.view")
    assert len(after) == before + 1


def test_memory_domain_view_telemetry(seeded_app):
    gid = _create_group_with_analyst("TVDom")
    did = _create_domain("tview-dom")
    _grant(gid, "memory_domain", did, "available")
    before = len(_events("memory_domain.view"))
    seeded_app["client"].get(
        "/api/memory/domains/tview-dom",
        headers=_auth(seeded_app["analyst_token"]),
    )
    after = _events("memory_domain.view")
    assert len(after) == before + 1


# -------- sync.pull_started ------------------------------------------------


def test_sync_pull_started_telemetry(seeded_app):
    before = len(_events("sync.pull_started"))
    seeded_app["client"].get(
        "/api/sync/manifest",
        headers=_auth(seeded_app["analyst_token"]),
    )
    after = _events("sync.pull_started")
    assert len(after) == before + 1
