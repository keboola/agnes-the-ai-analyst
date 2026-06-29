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


# -------- browse ----------------------------------------------------------


class TestStackBrowse:
    def test_browse_requires_auth(self, seeded_app):
        resp = seeded_app["client"].get("/api/stack/browse?type=data_package")
        assert resp.status_code == 401

    def test_browse_rejects_unknown_type(self, seeded_app):
        resp = seeded_app["client"].get(
            "/api/stack/browse?type=nope",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400

    def test_browse_lists_required_and_available(self, seeded_app):
        """Required → in_stack True even without a subscription; available →
        in_stack False until subscribed."""
        gid = _create_group_with_analyst("BrowseSales")
        req_id = _create_package("browse-req", "BrowseReq")
        avail_id = _create_package("browse-avail", "BrowseAvail")
        _grant(gid, "data_package", req_id, "required")
        _grant(gid, "data_package", avail_id, "available")
        resp = seeded_app["client"].get(
            "/api/stack/browse?type=data_package",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        by_id = {it["id"]: it for it in resp.json()["items"]}
        assert by_id[req_id]["in_stack"] is True
        assert by_id[req_id]["requirement"] == "required"
        assert by_id[avail_id]["in_stack"] is False
        assert by_id[avail_id]["requirement"] == "available"

    def test_browse_denies_session_principal(self, seeded_app):
        """A co-session token (SessionPrincipal) cannot manage the stack."""
        import asyncio

        from app.api.stack import browse_stack
        from app.auth.session_principal import SessionPrincipal
        from fastapi import HTTPException

        principal = SessionPrincipal(
            session_id="s1",
            participant_user_ids=["analyst1"],
            participant_emails=["analyst@test.com"],
            intersection={},
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(browse_stack(type="data_package", user=principal))
        assert exc.value.status_code == 403

    def test_browse_flips_in_stack_after_subscribe(self, seeded_app):
        gid = _create_group_with_analyst("BrowseFlip")
        pkg_id = _create_package("browse-flip", "BrowseFlip")
        _grant(gid, "data_package", pkg_id, "available")
        c = seeded_app["client"]
        # before: available, not in stack
        before = c.get(
            "/api/stack/browse?type=data_package",
            headers=_auth(seeded_app["analyst_token"]),
        ).json()["items"]
        assert next(it for it in before if it["id"] == pkg_id)["in_stack"] is False
        # subscribe
        c.post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        # after: in stack
        after = c.get(
            "/api/stack/browse?type=data_package",
            headers=_auth(seeded_app["analyst_token"]),
        ).json()["items"]
        assert next(it for it in after if it["id"] == pkg_id)["in_stack"] is True


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

    def test_subscribe_denies_session_principal(self, seeded_app):
        """A co-session token (SessionPrincipal) cannot manage the stack —
        regression for the guard gap flagged in #625 review (subscribe used
        to fall through to ``user[\"id\"]`` on the dataclass)."""
        import asyncio

        from app.api.stack import SubscribeRequest, subscribe
        from app.auth.session_principal import SessionPrincipal
        from fastapi import HTTPException

        principal = SessionPrincipal(
            session_id="s1",
            participant_user_ids=["analyst1"],
            participant_emails=["analyst@test.com"],
            intersection={},
        )
        payload = SubscribeRequest(resource_type="data_package", resource_id="p1")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(subscribe(payload=payload, user=principal))
        assert exc.value.status_code == 403


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
        # 0.54.26 design-rules pass bumped DELETE → 204 (idempotent
        # removal, empty body).
        assert resp.status_code == 204
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

    def test_unsubscribe_denies_session_principal(self, seeded_app):
        """Same guard as subscribe — unsubscribe must 403 a SessionPrincipal
        before touching ``user[\"id\"]``."""
        import asyncio

        from app.api.stack import unsubscribe
        from app.auth.session_principal import SessionPrincipal
        from fastapi import HTTPException

        principal = SessionPrincipal(
            session_id="s1",
            participant_user_ids=["analyst1"],
            participant_emails=["analyst@test.com"],
            intersection={},
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                unsubscribe(
                    resource_type="data_package", resource_id="p1", user=principal
                )
            )
        assert exc.value.status_code == 403
