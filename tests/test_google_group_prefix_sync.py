"""Tests for the Google-prefix mapping + system-group routing.

Covers:
- prefix filter (only `grp_acme_*` rows survive into user_group_members)
- login gate (302 when prefix is set and no Workspace group matches)
- system-group mapping (admin/everyone Workspace email → seeded
  Admin/Everyone row instead of a fresh user_groups insert)
- idempotency (second login produces the same memberships)
- API guard `_is_google_managed` + 409 google_managed_readonly
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def google_callback_env(tmp_path, monkeypatch):
    """TestClient for the Google callback wired against monkeypatched OAuth.

    Patches `is_available`, `oauth.google.authorize_access_token`, and
    `app.auth.group_sync.fetch_user_groups` so no real network traffic is
    required. The callback's domain check accepts `tester@example.com`
    because no `allowed_domains` is configured by default in tests.

    Per-test setup: monkeypatch the prefix/admin/everyone env vars and the
    `fetch_user_groups` return value before issuing the callback request.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")

    from app.main import create_app
    import app.auth.providers.google as g_mod

    monkeypatch.setattr(g_mod, "is_available", lambda: True)
    fake_oauth_google = SimpleNamespace(
        authorize_access_token=AsyncMock(
            return_value={
                "userinfo": {
                    "email": "tester@example.com",
                    "name": "Tester",
                }
            }
        )
    )
    monkeypatch.setattr(g_mod.oauth, "google", fake_oauth_google, raising=False)

    app = create_app()
    return {
        "client": TestClient(app, follow_redirects=False),
        "monkeypatch": monkeypatch,
        "g_mod": g_mod,
    }


def _set_fetch(monkeypatch, groups):
    import app.auth.group_sync as gs_mod
    monkeypatch.setattr(gs_mod, "fetch_user_groups", lambda email: list(groups))


def _system_db():
    from src.db import get_system_db
    return get_system_db()


class TestPrefixFilter:
    def test_prefix_filter_keeps_only_matching_groups(self, google_callback_env):
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        _set_fetch(env["monkeypatch"], [
            "grp_acme_finance@example.com",
            "grp_acme_eng@example.com",
            "grp_other@example.com",
            "acme-everyone@example.com",
            "drinks@example.com",
        ])

        resp = env["client"].get("/auth/google/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            assert user is not None

            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(
                user["id"]
            )
            ug = UserGroupsRepository(conn)
            names = sorted(ug.get(gid)["name"] for gid in group_ids)
            assert names == [
                "grp_acme_eng@example.com",
                "grp_acme_finance@example.com",
            ]
            for n in names:
                assert ug.get_by_name(n)["created_by"] == "system:google-sync"
        finally:
            conn.close()

    def test_prefix_set_no_match_redirects_to_login_error(
        self, google_callback_env
    ):
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        _set_fetch(env["monkeypatch"], [
            "drinks@example.com",
            "acme-everyone@example.com",
        ])

        resp = env["client"].get("/auth/google/callback?code=x&state=y")
        # Bare RedirectResponse defaults to 307 (matches the other error
        # redirects in google.py — domain_not_allowed, oauth_failed, etc.).
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/login?error=not_in_allowed_group"

        # No group memberships were written for the user (the gate fired
        # before replace_google_sync_groups). The user row may exist
        # because user creation happens before the gate — that's the
        # documented behavior; admins can mark the row inactive if they
        # want a hard block.
        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            if user:
                groups = UserGroupMembersRepository(conn).list_groups_for_user(
                    user["id"]
                )
                assert groups == []
        finally:
            conn.close()

    def test_no_prefix_means_legacy_behavior(self, google_callback_env):
        """Without AGNES_GOOGLE_GROUP_PREFIX, every fetched group is mirrored."""
        env = google_callback_env
        env["monkeypatch"].delenv("AGNES_GOOGLE_GROUP_PREFIX", raising=False)
        _set_fetch(env["monkeypatch"], [
            "grp_a@example.com",
            "grp_b@example.com",
        ])

        resp = env["client"].get("/auth/google/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(
                user["id"]
            )
            names = sorted(
                UserGroupsRepository(conn).get(gid)["name"] for gid in group_ids
            )
            assert names == ["grp_a@example.com", "grp_b@example.com"]
        finally:
            conn.close()


class TestSystemMapping:
    def test_admin_email_routes_to_seeded_admin_row(self, google_callback_env):
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        env["monkeypatch"].setenv(
            "AGNES_GROUP_ADMIN_EMAIL", "grp_acme_admin@example.com"
        )
        _set_fetch(env["monkeypatch"], [
            "grp_acme_admin@example.com",
            "grp_acme_finance@example.com",
        ])

        env["client"].get("/auth/google/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            ug = UserGroupsRepository(conn)
            # Crucially: no separate user_groups row was created with the
            # full admin email as `name`. Membership lands in the seeded
            # Admin row instead.
            assert ug.get_by_name("grp_acme_admin@example.com") is None

            admin_row = ug.get_by_name("Admin")
            assert admin_row is not None and admin_row["is_system"] is True

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(
                user["id"]
            )
            assert admin_row["id"] in group_ids

            # Finance group still goes through ensure() and creates a fresh row.
            finance = ug.get_by_name("grp_acme_finance@example.com")
            assert finance is not None
            assert finance["is_system"] is False
            assert finance["created_by"] == "system:google-sync"
            assert finance["id"] in group_ids
        finally:
            conn.close()

    def test_everyone_email_routes_to_seeded_everyone_row(
        self, google_callback_env
    ):
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        env["monkeypatch"].setenv(
            "AGNES_GROUP_EVERYONE_EMAIL", "grp_acme_everyone@example.com"
        )
        _set_fetch(env["monkeypatch"], [
            "grp_acme_everyone@example.com",
        ])

        env["client"].get("/auth/google/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            ug = UserGroupsRepository(conn)
            assert ug.get_by_name("grp_acme_everyone@example.com") is None

            everyone_row = ug.get_by_name("Everyone")
            assert everyone_row is not None
            assert everyone_row["is_system"] is True

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(
                user["id"]
            )
            assert everyone_row["id"] in group_ids
        finally:
            conn.close()


class TestIdempotency:
    def test_second_login_does_not_duplicate_groups(self, google_callback_env):
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        _set_fetch(env["monkeypatch"], [
            "grp_acme_finance@example.com",
        ])

        env["client"].get("/auth/google/callback?code=x&state=y")
        env["client"].get("/auth/google/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(
                user["id"]
            )
            # Exactly one membership: same group, deduplicated by the
            # (user_id, group_id) PK in user_group_members.
            assert len(group_ids) == 1

            # Exactly one user_groups row for that name (ensure() is
            # get-or-create, the second login picks up the existing row).
            count = conn.execute(
                "SELECT COUNT(*) FROM user_groups WHERE name = ?",
                ["grp_acme_finance@example.com"],
            ).fetchone()[0]
            assert count == 1
        finally:
            conn.close()


class TestIsGoogleManagedFlag:
    """Exercises the `_is_google_managed` rule used by GroupResponse +
    the API guard."""

    def test_google_sync_row_is_managed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from app.api.access import _is_google_managed
        g = {
            "name": "grp_acme_x@example.com",
            "is_system": False,
            "created_by": "system:google-sync",
        }
        assert _is_google_managed(g) is True

    def test_system_admin_with_env_mapping_is_managed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv(
            "AGNES_GROUP_ADMIN_EMAIL", "grp_acme_admin@example.com"
        )
        from app.api.access import _is_google_managed
        g = {"name": "Admin", "is_system": True, "created_by": "system:seed"}
        assert _is_google_managed(g) is True

    def test_system_admin_without_env_mapping_is_not_managed(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("AGNES_GROUP_ADMIN_EMAIL", raising=False)
        monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
        from app.api.access import _is_google_managed
        g = {"name": "Admin", "is_system": True, "created_by": "system:seed"}
        assert _is_google_managed(g) is False

    def test_manual_custom_group_is_not_managed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from app.api.access import _is_google_managed
        g = {
            "name": "data-team",
            "is_system": False,
            "created_by": "alice@example.com",
        }
        assert _is_google_managed(g) is False


class TestApiGuard:
    """API endpoints reject mutations on Google-managed groups with 409."""

    @pytest.fixture
    def admin_client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setenv(
            "AGNES_GROUP_ADMIN_EMAIL", "grp_acme_admin@example.com"
        )

        from app.main import create_app
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import (
            UserGroupMembersRepository,
        )
        from app.auth.jwt import create_access_token

        conn = get_system_db()
        try:
            ur = UserRepository(conn)
            ur.create(id="admin1", email="admin@x", name="Admin1", role="admin")
            ur.create(id="u1", email="u1@x", name="U1", role="analyst")
            ug = UserGroupsRepository(conn)
            admin_id = ug.get_by_name("Admin")["id"]
            UserGroupMembersRepository(conn).add_member(
                "admin1", admin_id, source="system_seed",
            )
            # A google-sync group to act on.
            ug.ensure("grp_acme_finance@example.com")
        finally:
            conn.close()

        app = create_app()
        client = TestClient(app, follow_redirects=False)
        token = create_access_token("admin1", "admin@x", "")
        client.cookies.set("access_token", token)
        return client

    def _gid(self, name):
        from src.db import get_system_db
        from src.repositories.user_groups import UserGroupsRepository
        conn = get_system_db()
        try:
            return UserGroupsRepository(conn).get_by_name(name)["id"]
        finally:
            conn.close()

    def test_patch_google_managed_returns_409(self, admin_client):
        gid = self._gid("grp_acme_finance@example.com")
        r = admin_client.patch(
            f"/api/admin/groups/{gid}",
            json={"name": "renamed"},
        )
        assert r.status_code == 409
        body = r.json()
        # FastAPI wraps the dict detail under "detail"; assert the code is
        # surfaced for the UI's machine-readable branch.
        assert body["detail"]["code"] == "google_managed_readonly"

    def test_delete_google_managed_returns_409(self, admin_client):
        gid = self._gid("grp_acme_finance@example.com")
        r = admin_client.delete(f"/api/admin/groups/{gid}")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "google_managed_readonly"

    def test_add_member_to_google_managed_returns_409(self, admin_client):
        gid = self._gid("grp_acme_finance@example.com")
        r = admin_client.post(
            f"/api/admin/groups/{gid}/members",
            json={"email": "u1@x"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "google_managed_readonly"

    def test_patch_admin_with_env_mapping_returns_409(self, admin_client):
        # AGNES_GROUP_ADMIN_EMAIL is set in the fixture → seeded Admin row
        # is treated as Google-managed and rejects renames here too.
        gid = self._gid("Admin")
        r = admin_client.patch(
            f"/api/admin/groups/{gid}",
            json={"description": "updated"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "google_managed_readonly"
