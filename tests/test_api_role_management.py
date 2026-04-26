"""Tests for the v9 role-management REST API.

Endpoints under ``/api/admin``:

- ``GET    /internal-roles``
- ``GET    /group-mappings``
- ``POST   /group-mappings``
- ``DELETE /group-mappings/{mapping_id}``
- ``GET    /users/{user_id}/role-grants``
- ``POST   /users/{user_id}/role-grants``
- ``DELETE /users/{user_id}/role-grants/{grant_id}``
- ``GET    /users/{user_id}/effective-roles``

Auth model: every endpoint is gated by
``require_internal_role("core.admin")``. The shared ``seeded_app`` fixture
issues JWT (non-PAT) tokens; the resolver's ``user_role_grants`` fallback
satisfies the gate because the seeded admin user holds ``core.admin`` via
the v9 grant inserted by ``UserRepository.create``.
"""

import uuid

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _audit_count(conn, action: str) -> int:
    """Number of audit_log rows with the given action — used to assert
    that a mutator wrote a row OR (negation case) didn't."""
    result = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = ?", [action]
    ).fetchone()
    return int(result[0]) if result else 0


def _get_role_id(conn, key: str) -> str:
    row = conn.execute(
        "SELECT id FROM internal_roles WHERE key = ?", [key]
    ).fetchone()
    assert row, f"core role {key} should be seeded"
    return row[0]


# --- /internal-roles ---------------------------------------------------------

class TestListInternalRoles:
    def test_lists_seeded_core_roles(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/internal-roles", headers=_auth(seeded_app["admin_token"])
        )
        assert resp.status_code == 200
        keys = {r["key"] for r in resp.json()}
        # The v9 backfill seeds the four core.* hierarchy rows on every
        # connect — every fresh DB should expose them via this endpoint.
        assert {"core.viewer", "core.analyst", "core.km_admin", "core.admin"} <= keys

    def test_implies_returned_as_list(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/internal-roles", headers=_auth(seeded_app["admin_token"])
        )
        assert resp.status_code == 200
        admin_row = next(r for r in resp.json() if r["key"] == "core.admin")
        # Stored as JSON-as-VARCHAR — handler must decode for the wire.
        assert isinstance(admin_row["implies"], list)
        assert "core.km_admin" in admin_row["implies"]
        assert admin_row["is_core"] is True

    def test_403_for_non_admin(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/internal-roles",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_401_when_unauthenticated(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/api/admin/internal-roles")
        assert resp.status_code == 401


# --- /group-mappings ---------------------------------------------------------

class TestGroupMappingsRead:
    def test_list_empty(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/group-mappings",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_403_for_analyst(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/group-mappings",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestGroupMappingCreate:
    def test_creates_and_audits(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/group-mappings",
            json={
                "external_group_id": "engineers@example.com",
                "role_key": "core.analyst",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["external_group_id"] == "engineers@example.com"
        assert body["role_key"] == "core.analyst"
        assert body["role_display_name"] == "Analyst"

        # Audit row must exist with action=role_mapping.created.
        from src.db import get_system_db
        conn = get_system_db()
        try:
            assert _audit_count(conn, "role_mapping.created") >= 1
        finally:
            conn.close()

    def test_unknown_role_key_returns_404(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/group-mappings",
            json={
                "external_group_id": "engineers@example.com",
                "role_key": "core.nonexistent",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404
        assert "core.nonexistent" in resp.json()["detail"]

    def test_duplicate_returns_409(self, seeded_app):
        c = seeded_app["client"]
        payload = {
            "external_group_id": "ops@example.com",
            "role_key": "core.viewer",
        }
        first = c.post(
            "/api/admin/group-mappings",
            json=payload,
            headers=_auth(seeded_app["admin_token"]),
        )
        assert first.status_code == 201
        second = c.post(
            "/api/admin/group-mappings",
            json=payload,
            headers=_auth(seeded_app["admin_token"]),
        )
        assert second.status_code == 409

    def test_403_for_analyst(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/group-mappings",
            json={
                "external_group_id": "engineers@example.com",
                "role_key": "core.analyst",
            },
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_listing_after_create_includes_mapping(self, seeded_app):
        c = seeded_app["client"]
        c.post(
            "/api/admin/group-mappings",
            json={
                "external_group_id": "data-team@example.com",
                "role_key": "core.km_admin",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        listing = c.get(
            "/api/admin/group-mappings",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert listing.status_code == 200
        ids = {row["external_group_id"] for row in listing.json()}
        assert "data-team@example.com" in ids


class TestGroupMappingDelete:
    def test_deletes_and_audits(self, seeded_app):
        c = seeded_app["client"]
        # Create first.
        created = c.post(
            "/api/admin/group-mappings",
            json={
                "external_group_id": "to-delete@example.com",
                "role_key": "core.viewer",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        mapping_id = created.json()["id"]

        # Delete.
        resp = c.delete(
            f"/api/admin/group-mappings/{mapping_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 204

        # 404 on second delete proves the row is gone.
        again = c.delete(
            f"/api/admin/group-mappings/{mapping_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert again.status_code == 404

        from src.db import get_system_db
        conn = get_system_db()
        try:
            assert _audit_count(conn, "role_mapping.deleted") >= 1
        finally:
            conn.close()

    def test_404_for_unknown_id(self, seeded_app):
        c = seeded_app["client"]
        resp = c.delete(
            f"/api/admin/group-mappings/{uuid.uuid4()}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404

    def test_403_for_analyst(self, seeded_app):
        c = seeded_app["client"]
        resp = c.delete(
            f"/api/admin/group-mappings/{uuid.uuid4()}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


# --- /users/{user_id}/role-grants -------------------------------------------

class TestRoleGrantsRead:
    def test_lists_seeded_admin_grants(self, seeded_app):
        """The seeded admin user has a core.admin grant inserted by
        UserRepository.create -> _grant_core_role. The endpoint must
        surface it."""
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/users/admin1/role-grants",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        keys = {r["role_key"] for r in resp.json()}
        assert "core.admin" in keys

    def test_404_for_unknown_user(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/users/does-not-exist/role-grants",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404

    def test_403_for_analyst(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/users/admin1/role-grants",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestRoleGrantsCreate:
    def test_grants_role_and_audits(self, seeded_app):
        c = seeded_app["client"]
        # analyst1 is seeded with core.analyst — promote them to core.km_admin.
        resp = c.post(
            "/api/admin/users/analyst1/role-grants",
            json={"role_key": "core.km_admin"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["role_key"] == "core.km_admin"
        assert body["source"] == "direct"
        assert body["user_id"] == "analyst1"

        from src.db import get_system_db
        conn = get_system_db()
        try:
            assert _audit_count(conn, "role_grant.created") >= 1
        finally:
            conn.close()

    def test_409_when_already_granted(self, seeded_app):
        c = seeded_app["client"]
        # analyst1 already holds core.analyst from seed_app's user creation.
        resp = c.post(
            "/api/admin/users/analyst1/role-grants",
            json={"role_key": "core.analyst"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409

    def test_404_for_unknown_user(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/users/ghost/role-grants",
            json={"role_key": "core.viewer"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404

    def test_404_for_unknown_role(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/users/analyst1/role-grants",
            json={"role_key": "core.bogus"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404

    def test_403_for_analyst(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/users/analyst1/role-grants",
            json={"role_key": "core.km_admin"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestRoleGrantsDelete:
    def test_revokes_grant_and_audits(self, seeded_app):
        c = seeded_app["client"]
        # Find the analyst's seeded core.analyst grant, then delete it.
        grants = c.get(
            "/api/admin/users/analyst1/role-grants",
            headers=_auth(seeded_app["admin_token"]),
        ).json()
        analyst_grant = next(g for g in grants if g["role_key"] == "core.analyst")
        grant_id = analyst_grant["id"]

        resp = c.delete(
            f"/api/admin/users/analyst1/role-grants/{grant_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 204

        from src.db import get_system_db
        conn = get_system_db()
        try:
            assert _audit_count(conn, "role_grant.deleted") >= 1
        finally:
            conn.close()

    def test_404_for_unknown_grant(self, seeded_app):
        c = seeded_app["client"]
        resp = c.delete(
            f"/api/admin/users/analyst1/role-grants/{uuid.uuid4()}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404

    def test_404_when_grant_belongs_to_other_user(self, seeded_app):
        """Path consistency: a grant id legitimately exists, but for a
        different user. Endpoint must 404 rather than reveal the row to
        the wrong path so admins see a consistent contract."""
        c = seeded_app["client"]
        # Find the admin's core.admin grant.
        admin_grants = c.get(
            "/api/admin/users/admin1/role-grants",
            headers=_auth(seeded_app["admin_token"]),
        ).json()
        admin_grant_id = admin_grants[0]["id"]

        # Try to delete it via analyst1's path.
        resp = c.delete(
            f"/api/admin/users/analyst1/role-grants/{admin_grant_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404

    def test_403_for_analyst(self, seeded_app):
        c = seeded_app["client"]
        resp = c.delete(
            f"/api/admin/users/analyst1/role-grants/{uuid.uuid4()}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_refuses_to_delete_last_admin_grant(self, seeded_app):
        """Lockout-prevention: deleting the only active core.admin grant
        would leave the system without anyone able to call admin endpoints.
        The handler must refuse with 4xx and the audit row must be absent."""
        c = seeded_app["client"]
        # Find admin1's core.admin grant.
        admin_grants = c.get(
            "/api/admin/users/admin1/role-grants",
            headers=_auth(seeded_app["admin_token"]),
        ).json()
        admin_grant = next(
            g for g in admin_grants if g["role_key"] == "core.admin"
        )
        grant_id = admin_grant["id"]

        from src.db import get_system_db
        conn = get_system_db()
        try:
            before = _audit_count(conn, "role_grant.deleted")
        finally:
            conn.close()

        resp = c.delete(
            f"/api/admin/users/admin1/role-grants/{grant_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        # 409 — same code UserRepository.update uses for "last admin"
        # protection, keeps the contract uniform across role-CRUD surfaces.
        assert resp.status_code == 409
        assert "last" in resp.json()["detail"].lower()

        # Audit row must NOT have been written for the failed delete.
        conn = get_system_db()
        try:
            after = _audit_count(conn, "role_grant.deleted")
        finally:
            conn.close()
        assert after == before, (
            "Audit row written for refused delete — handler should bail "
            "before _audit() runs when count_admins guard trips."
        )

    def test_can_delete_admin_grant_when_multiple_admins_exist(
        self, seeded_app,
    ):
        """Inverse of the lockout test: with two active admins, deleting one
        of their core.admin grants must succeed."""
        # Promote analyst1 to core.admin via the API.
        c = seeded_app["client"]
        promote = c.post(
            "/api/admin/users/analyst1/role-grants",
            json={"role_key": "core.admin"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert promote.status_code == 201
        new_grant_id = promote.json()["id"]

        # Now delete the new grant — count_admins is 2 so it should pass.
        resp = c.delete(
            f"/api/admin/users/analyst1/role-grants/{new_grant_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 204


# --- /users/{user_id}/effective-roles ----------------------------------------

class TestEffectiveRoles:
    def test_returns_direct_grants_and_expanded_keys(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/users/admin1/effective-roles",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()

        # Direct list mirrors the role-grants listing.
        direct_keys = {g["role_key"] for g in body["direct"]}
        assert "core.admin" in direct_keys

        # Expanded includes implies hierarchy: admin -> km_admin ->
        # analyst -> viewer.
        assert "core.admin" in body["expanded"]
        assert "core.km_admin" in body["expanded"]
        assert "core.analyst" in body["expanded"]
        assert "core.viewer" in body["expanded"]

        # group is intentionally [] — see endpoint docstring.
        assert body["group"] == []

    def test_404_for_unknown_user(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/users/no-such-user/effective-roles",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404

    def test_403_for_analyst(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/api/admin/users/admin1/effective-roles",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


# --- Repository smoke tests --------------------------------------------------

class TestUserRoleGrantsRepository:
    """Light coverage for the new repository — full CRUD smoke."""

    def test_create_list_delete(self, seeded_app):
        from src.db import get_system_db
        from src.repositories.user_role_grants import UserRoleGrantsRepository

        conn = get_system_db()
        try:
            repo = UserRoleGrantsRepository(conn)
            # analyst1 has its core.analyst grant from seeding; pin behavior.
            grants = repo.list_for_user("analyst1")
            assert len(grants) >= 1
            assert grants[0]["role_key"] == "core.analyst"

            # Insert another grant pointing at core.viewer.
            viewer_id = _get_role_id(conn, "core.viewer")
            new_id = str(uuid.uuid4())
            repo.create(
                id=new_id,
                user_id="analyst1",
                internal_role_id=viewer_id,
                granted_by="test",
                source="direct",
            )

            after = repo.list_for_user("analyst1")
            keys = {g["role_key"] for g in after}
            assert {"core.analyst", "core.viewer"} <= keys

            # list_by_role finds the grant.
            holders = repo.list_by_role(viewer_id)
            holder_users = {h["user_id"] for h in holders}
            assert "analyst1" in holder_users

            # Delete by id.
            repo.delete(new_id)
            assert repo.get(new_id) is None
        finally:
            conn.close()

    def test_delete_for_user(self, seeded_app):
        from src.db import get_system_db
        from src.repositories.user_role_grants import UserRoleGrantsRepository

        conn = get_system_db()
        try:
            repo = UserRoleGrantsRepository(conn)
            # Wipe analyst1's grants — delete_for_user is the cascade helper
            # used by user-deletion paths.
            repo.delete_for_user("analyst1")
            assert repo.list_for_user("analyst1") == []
        finally:
            conn.close()
