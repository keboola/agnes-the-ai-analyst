"""Tests for /api/stack — subscribe / unsubscribe / list (Task 6.3).

Covers RBAC (no grant → 403), business-rule errors (already_required,
cannot_remove_required), and telemetry (stack.subscribe / stack.unsubscribe
rows in usage_events).
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


def _create_group_with_analyst(name: str = "Sales") -> str:
    """Create a group, add analyst1 to it, return group_id."""
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    g = UserGroupsRepository(conn).create(name=name, description="", created_by="test")
    gid = g["id"] if isinstance(g, dict) else g
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.close()
    return gid


def _create_package(slug: str = "p", name: str = "P") -> str:
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    pkg_id = DataPackagesRepository(conn).create(
        name=name, slug=slug, description=None,
        icon=None, color=None, created_by="test",
    )
    conn.close()
    return pkg_id


def _telemetry_for(event_type: str, user_id: str) -> list[dict]:
    conn = get_system_db()
    rows = conn.execute(
        "SELECT event_type, friction_tags FROM usage_events "
        "WHERE event_type = ? AND user_id = ? AND source = 'server' "
        "ORDER BY occurred_at",
        [event_type, user_id],
    ).fetchall()
    conn.close()
    import json
    return [
        {"event_type": e, "props": json.loads(f) if f else None}
        for e, f in rows
    ]


# -------- list ------------------------------------------------------------


class TestStackList:
    def test_list_requires_auth(self, seeded_app):
        resp = seeded_app["client"].get("/api/stack?type=data_package")
        assert resp.status_code == 401

    def test_list_rejects_unknown_type(self, seeded_app):
        resp = seeded_app["client"].get(
            "/api/stack?type=nope",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400

    def test_list_rejects_marketplace_plugin_type(self, seeded_app):
        """D1 — marketplace plugins keep their own resolver. /api/stack
        refuses to serve them."""
        resp = seeded_app["client"].get(
            "/api/stack?type=marketplace_plugin",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400

    def test_list_returns_subscribed(self, seeded_app):
        gid = _create_group_with_analyst("ListSales")
        pkg_id = _create_package("list-pkg", "ListPkg")
        _grant(gid, "data_package", pkg_id, "available")
        c = seeded_app["client"]
        # subscribe first
        c.post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        resp = c.get(
            "/api/stack?type=data_package",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        ids = [it["id"] for it in resp.json()["items"]]
        assert pkg_id in ids

    def test_list_includes_required_even_without_subscription(self, seeded_app):
        gid = _create_group_with_analyst("ReqSales")
        pkg_id = _create_package("req-pkg", "ReqPkg")
        _grant(gid, "data_package", pkg_id, "required")
        resp = seeded_app["client"].get(
            "/api/stack?type=data_package",
            headers=_auth(seeded_app["analyst_token"]),
        )
        items = resp.json()["items"]
        req = next(it for it in items if it["id"] == pkg_id)
        assert req["requirement"] == "required"
        assert req["in_stack"] is True


# -------- subscribe -------------------------------------------------------


class TestStackSubscribe:
    def test_subscribe_emits_telemetry(self, seeded_app):
        gid = _create_group_with_analyst("SubSales")
        pkg_id = _create_package("sub-pkg", "SubPkg")
        _grant(gid, "data_package", pkg_id, "available")
        resp = seeded_app["client"].post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        events = _telemetry_for("stack.subscribe", "analyst1")
        assert any(e["props"]["resource_id"] == pkg_id for e in events)

    def test_subscribe_without_grant_is_403(self, seeded_app):
        """No grant = no access; we don't leak existence by subscribing."""
        pkg_id = _create_package("ghost-pkg", "GhostPkg")
        resp = seeded_app["client"].post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_subscribe_to_required_is_400_already_required(self, seeded_app):
        gid = _create_group_with_analyst("ReqGrp")
        pkg_id = _create_package("already-req", "AlreadyReq")
        _grant(gid, "data_package", pkg_id, "required")
        resp = seeded_app["client"].post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "already_required"


# -------- unsubscribe -----------------------------------------------------


class TestStackUnsubscribe:
    def test_unsubscribe_emits_telemetry(self, seeded_app):
        gid = _create_group_with_analyst("UnsubSales")
        pkg_id = _create_package("unsub-pkg", "UnsubPkg")
        _grant(gid, "data_package", pkg_id, "available")
        c = seeded_app["client"]
        c.post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        resp = c.delete(
            f"/api/stack/subscription/data_package/{pkg_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        events = _telemetry_for("stack.unsubscribe", "analyst1")
        assert any(e["props"]["resource_id"] == pkg_id for e in events)

    def test_unsubscribe_required_is_400(self, seeded_app):
        gid = _create_group_with_analyst("UnsubReq")
        pkg_id = _create_package("unsub-req", "UnsubReq")
        _grant(gid, "data_package", pkg_id, "required")
        resp = seeded_app["client"].delete(
            f"/api/stack/subscription/data_package/{pkg_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "cannot_remove_required"
