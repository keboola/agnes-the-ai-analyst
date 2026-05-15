"""End-to-end RBAC integration tests for the v49 unified stack.

Validates the seven canonical access scenarios from Section 10.7 of the
unified-stack design — together they form the "is this RBAC actually
wired through every layer" sanity gate:

1. **Auto-membership in ``Everyone``** + grant ``requirement='required'``
   on ``Everyone`` → resource lands in every user's effective stack.
2. **Admin god-mode** short-circuit: admin sees all resources regardless
   of grants on ``/api/stack?type=…``.
3. **Non-admin user without any MEMORY_DOMAIN grant** opens
   ``/corporate-memory`` → page renders, Browse is empty + the
   "ask your admin" banner is present.
4. **Available grant + subscription** appears in stack; revoking the
   grant filters the now-zombie subscription back out.
5. **Required grant** can't be unsubscribed via ``/api/stack/subscription``
   (400 ``cannot_remove_required``).
6. **No grant at all** → ``POST /api/stack/subscribe`` returns 403.
7. **Soft downgrade**: admin flips ``required → available``; previously
   required users keep the resource in their stack via the eager
   ``user_stack_subscriptions`` materialization.

Each scenario hits the real FastAPI ``TestClient`` so the full middleware
stack (auth, audit, telemetry) runs.
"""

from __future__ import annotations

import uuid

import pytest

from src.db import get_system_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _everyone_id(conn) -> str:
    return conn.execute(
        "SELECT id FROM user_groups WHERE name = 'Everyone'"
    ).fetchone()[0]


def _add_everyone_membership(conn, user_id: str) -> None:
    """Auto-membership in Everyone for ``user_id``. ``seeded_app`` does NOT
    do this; the test creates the row explicitly to model the production
    sign-up flow that auto-adds new users."""
    gid = _everyone_id(conn)
    conn.execute(
        "INSERT OR IGNORE INTO user_group_members(user_id, group_id, source) "
        "VALUES (?, ?, 'system_seed')",
        [user_id, gid],
    )


def _seed_grant(
    conn, *, group_id: str, resource_type: str, resource_id: str,
    requirement: str = "available",
) -> str:
    grant_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'test')",
        [grant_id, group_id, resource_type, resource_id, requirement],
    )
    return grant_id


def _seed_pkg(conn, slug: str, name: str = "Pkg") -> str:
    from src.repositories.data_packages import DataPackagesRepository

    return DataPackagesRepository(conn).create(
        name=name, slug=slug, description=None,
        icon=None, color=None, created_by="test",
    )


def _seed_group_with(conn, *, name: str, user_ids: list[str]) -> str:
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    g = UserGroupsRepository(conn).create(name=name, description="", created_by="test")
    gid = g["id"] if isinstance(g, dict) else g
    members = UserGroupMembersRepository(conn)
    for uid in user_ids:
        members.add_member(uid, gid, source="test")
    return gid


# ---------------------------------------------------------------------------
# Scenario 1 — Everyone + required → every user gets it
# ---------------------------------------------------------------------------


class TestEveryoneRequiredAutomembership:
    def test_required_on_everyone_lands_in_every_user_stack(self, seeded_app):
        conn = get_system_db()
        # All four seeded users join Everyone.
        for uid in ("admin1", "km_admin1", "analyst1", "viewer1"):
            _add_everyone_membership(conn, uid)
        pkg_id = _seed_pkg(conn, "all-hands-pkg", "All Hands")
        _seed_grant(
            conn,
            group_id=_everyone_id(conn),
            resource_type="data_package",
            resource_id=pkg_id,
            requirement="required",
        )
        conn.close()

        for token_key in ("analyst_token", "viewer_token", "km_admin_token"):
            r = seeded_app["client"].get(
                "/api/stack?type=data_package",
                headers=_auth(seeded_app[token_key]),
            )
            assert r.status_code == 200, f"{token_key}: {r.text}"
            items = r.json()["items"]
            match = next((it for it in items if it["id"] == pkg_id), None)
            assert match is not None, (
                f"{token_key} should see the required Everyone package in stack; "
                f"got items={items}"
            )
            assert match["requirement"] == "required"
            assert match["in_stack"] is True


# ---------------------------------------------------------------------------
# Scenario 2 — Admin god-mode short-circuit on /api/stack
# ---------------------------------------------------------------------------


class TestAdminGodMode:
    def test_admin_sees_unrelated_packages_without_explicit_grant(self, seeded_app):
        """Admin doesn't need to be granted anything to see resources via the
        regular ``/api/stack`` endpoint. The resolver itself doesn't
        short-circuit on admin (it's a generic grants-driven projection),
        but the web view DOES — `/corporate-memory` shows every domain for
        admins regardless of grants. The matching invariant for /api/stack
        is that an admin can sub to ANY package without a 403; non-admins
        without a grant get 403."""
        conn = get_system_db()
        pkg_id = _seed_pkg(conn, "admin-only-pkg")
        conn.close()

        # Non-admin: no grant → 403.
        r_analyst = seeded_app["client"].post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r_analyst.status_code == 403

        # Admin: same package, no grant → can subscribe (god-mode bypass in
        # the underlying `can_access_*` check + the resolver's add path).
        r_admin = seeded_app["client"].post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r_admin.status_code == 200, r_admin.text


# ---------------------------------------------------------------------------
# Scenario 3 — Non-admin on /corporate-memory without grant
# ---------------------------------------------------------------------------


class TestEmptyMemoryBrowseBanner:
    def test_non_admin_with_no_memory_grant_sees_empty_banner(self, seeded_app):
        conn = get_system_db()
        # No memory_domain grants on analyst1's groups. Page should render
        # with an empty-state banner directing them to ask the admin.
        _add_everyone_membership(conn, "analyst1")
        conn.close()

        r = seeded_app["client"].get(
            "/corporate-memory",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.text
        assert "No memory domains assigned" in body, body[:1000]
        assert "Ask your admin to grant" in body


# ---------------------------------------------------------------------------
# Scenario 4 — Available + sub → stack; revoke grant → zombie filtered out
# ---------------------------------------------------------------------------


class TestZombieSubscriptionFiltered:
    def test_revoke_grant_filters_out_zombie_subscription(self, seeded_app):
        conn = get_system_db()
        gid = _seed_group_with(conn, name="zombie_grp", user_ids=["analyst1"])
        pkg_id = _seed_pkg(conn, "zombie-pkg")
        grant_id = _seed_grant(
            conn, group_id=gid, resource_type="data_package",
            resource_id=pkg_id, requirement="available",
        )
        conn.close()

        c = seeded_app["client"]
        # Subscribe.
        r = c.post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text

        # In stack.
        r = c.get(
            "/api/stack?type=data_package",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert any(it["id"] == pkg_id for it in r.json()["items"])

        # Admin revokes the grant.
        r = c.delete(
            f"/api/admin/grants/{grant_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 204, r.text

        # Stack no longer includes the package — the subscription survives
        # in `user_stack_subscriptions` but the resolver intersects with
        # available_ids, which now excludes the package.
        r = c.get(
            "/api/stack?type=data_package",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert not any(it["id"] == pkg_id for it in r.json()["items"]), (
            f"zombie subscription leaked into stack: {r.json()}"
        )


# ---------------------------------------------------------------------------
# Scenario 5 — Required can't be unsubscribed
# ---------------------------------------------------------------------------


class TestRequiredCannotBeRemoved:
    def test_unsubscribe_required_is_400(self, seeded_app):
        conn = get_system_db()
        gid = _seed_group_with(conn, name="must_have_grp", user_ids=["analyst1"])
        pkg_id = _seed_pkg(conn, "must-have-pkg")
        _seed_grant(
            conn, group_id=gid, resource_type="data_package",
            resource_id=pkg_id, requirement="required",
        )
        conn.close()

        r = seeded_app["client"].delete(
            f"/api/stack/subscription/data_package/{pkg_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "cannot_remove_required"


# ---------------------------------------------------------------------------
# Scenario 6 — No grant → subscribe is 403
# ---------------------------------------------------------------------------


class TestNoGrantSubscribeBlocked:
    def test_subscribe_without_any_grant_is_403(self, seeded_app):
        conn = get_system_db()
        pkg_id = _seed_pkg(conn, "stranger-pkg")
        conn.close()

        r = seeded_app["client"].post(
            "/api/stack/subscribe",
            json={"resource_type": "data_package", "resource_id": pkg_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Scenario 7 — Soft downgrade required → available preserves users' stacks
# ---------------------------------------------------------------------------


class TestSoftDowngradePreservesUserStack:
    def test_required_to_available_keeps_user_stack(self, seeded_app):
        conn = get_system_db()
        gid = _seed_group_with(
            conn, name="downgrade_grp",
            user_ids=["analyst1", "viewer1", "km_admin1"],
        )
        pkg_id = _seed_pkg(conn, "downgrade-pkg")
        grant_id = _seed_grant(
            conn, group_id=gid, resource_type="data_package",
            resource_id=pkg_id, requirement="required",
        )
        conn.close()

        c = seeded_app["client"]

        # Every member sees the package as required → in_stack=True.
        for token_key in ("analyst_token", "viewer_token", "km_admin_token"):
            r = c.get(
                "/api/stack?type=data_package",
                headers=_auth(seeded_app[token_key]),
            )
            assert any(
                it["id"] == pkg_id and it["requirement"] == "required"
                for it in r.json()["items"]
            )

        # Admin downgrades.
        r = c.put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "available"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200

        # The eager materialize wrote a subscription row for each member,
        # so they still see the package — but now as ``available + in_stack``.
        for token_key in ("analyst_token", "viewer_token", "km_admin_token"):
            r = c.get(
                "/api/stack?type=data_package",
                headers=_auth(seeded_app[token_key]),
            )
            items = r.json()["items"]
            match = next((it for it in items if it["id"] == pkg_id), None)
            assert match is not None, (
                f"{token_key} lost the package after soft downgrade: {items}"
            )
            assert match["requirement"] == "available"
            assert match["in_stack"] is True

        # And the user_stack_subscriptions table actually has the rows
        # (direct DB assert keeps the resolver honest).
        conn = get_system_db()
        sub_users = {
            r[0] for r in conn.execute(
                "SELECT user_id FROM user_stack_subscriptions "
                "WHERE resource_type='data_package' AND resource_id=?",
                [pkg_id],
            ).fetchall()
        }
        conn.close()
        assert sub_users == {"analyst1", "viewer1", "km_admin1"}
