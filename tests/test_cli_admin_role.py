"""Tests for da admin role-management subcommands.

Covers the CLI surface added on top of /api/admin/internal-roles,
/api/admin/group-mappings, and /api/admin/users/{id}/role-grants — see
app/api/role_management.py (sibling change).

Two layers:
- Mock-based unit tests for the CLI dispatch + output formatting.
- Optional integration tests against an in-process FastAPI TestClient
  authenticated with a PAT. They auto-skip when the role_management
  router has not yet been registered (e.g. when this file lands before
  the API agent's commit).
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_cli_config(tmp_path, monkeypatch):
    """Point the CLI at an empty config dir so we never touch real ~/.config."""
    monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DA_NO_UPDATE_CHECK", "1")
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    """Build a MagicMock that quacks like an httpx.Response."""
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


# ---- role list / show ----


class TestRoleList:
    def test_role_list_text_finds_core_admin(self):
        roles = [
            {
                "key": "core.admin", "display_name": "Administrator",
                "owner_module": "core", "is_core": True,
                "implies": ["core.km_admin", "core.analyst", "core.viewer"],
            },
            {
                "key": "core.analyst", "display_name": "Analyst",
                "owner_module": "core", "is_core": True, "implies": ["core.viewer"],
            },
        ]
        with patch("cli.commands.admin.api_get", return_value=_resp(200, roles)):
            result = runner.invoke(app, ["admin", "role", "list"])
        assert result.exit_code == 0, result.output
        assert "core.admin" in result.output
        assert "Administrator" in result.output
        assert "Internal roles: 2" in result.output

    def test_role_list_handles_object_envelope(self):
        """Tolerate both list and {roles: [...]} response shapes."""
        with patch(
            "cli.commands.admin.api_get",
            return_value=_resp(200, {"roles": [{"key": "core.admin", "display_name": "A"}]}),
        ):
            result = runner.invoke(app, ["admin", "role", "list"])
        assert result.exit_code == 0
        assert "core.admin" in result.output

    def test_role_list_json(self):
        roles = [{"key": "core.admin", "display_name": "Administrator"}]
        with patch("cli.commands.admin.api_get", return_value=_resp(200, roles)):
            result = runner.invoke(app, ["admin", "role", "list", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed[0]["key"] == "core.admin"

    def test_role_list_empty(self):
        with patch("cli.commands.admin.api_get", return_value=_resp(200, [])):
            result = runner.invoke(app, ["admin", "role", "list"])
        assert result.exit_code == 0
        assert "No internal roles" in result.output

    def test_role_list_unauthorized_exits_nonzero(self):
        with patch(
            "cli.commands.admin.api_get",
            return_value=_resp(401, {"detail": "Not authenticated"}),
        ):
            result = runner.invoke(app, ["admin", "role", "list"])
        assert result.exit_code == 1
        assert "Not authenticated" in result.output

    def test_role_list_forbidden_exits_nonzero(self):
        with patch(
            "cli.commands.admin.api_get",
            return_value=_resp(403, {"detail": "Requires internal role 'core.admin'"}),
        ):
            result = runner.invoke(app, ["admin", "role", "list"])
        assert result.exit_code == 1
        assert "core.admin" in result.output


class TestRoleShow:
    def test_role_show_with_mappings(self):
        roles = [
            {"key": "core.admin", "display_name": "Administrator",
             "owner_module": "core", "is_core": True,
             "implies": ["core.km_admin"]},
        ]
        mappings = [
            {"id": "m1", "external_group_id": "grp:devs", "role_key": "core.admin"},
            {"id": "m2", "external_group_id": "grp:other", "role_key": "core.analyst"},
        ]
        with patch(
            "cli.commands.admin.api_get",
            side_effect=[_resp(200, roles), _resp(200, mappings)],
        ):
            result = runner.invoke(app, ["admin", "role", "show", "core.admin"])
        assert result.exit_code == 0, result.output
        assert "core.admin" in result.output
        assert "Administrator" in result.output
        assert "core.km_admin" in result.output  # implies row
        assert "mappings     : 1" in result.output  # only m1 matches

    def test_role_show_not_found(self):
        with patch(
            "cli.commands.admin.api_get",
            return_value=_resp(200, [{"key": "core.viewer"}]),
        ):
            result = runner.invoke(app, ["admin", "role", "show", "core.admin"])
        assert result.exit_code == 1
        assert "Role not found" in result.output

    def test_role_show_implies_string_form(self):
        """API may return implies as JSON-encoded string; we should decode."""
        roles = [{
            "key": "core.admin", "display_name": "Admin",
            "implies": '["core.km_admin", "core.analyst"]',
        }]
        with patch(
            "cli.commands.admin.api_get",
            side_effect=[_resp(200, roles), _resp(200, [])],
        ):
            result = runner.invoke(app, ["admin", "role", "show", "core.admin"])
        assert result.exit_code == 0
        assert "core.km_admin" in result.output
        assert "core.analyst" in result.output


# ---- mapping CRUD round-trip ----


class TestMappingRoundtrip:
    def test_mapping_list_text(self):
        mappings = [
            {"id": "m-1", "external_group_id": "grp:eng",
             "role_key": "core.admin", "assigned_by": "admin@x.com",
             "created_at": "2026-04-26T10:00:00Z"},
        ]
        with patch("cli.commands.admin.api_get", return_value=_resp(200, mappings)):
            result = runner.invoke(app, ["admin", "mapping", "list"])
        assert result.exit_code == 0
        assert "grp:eng" in result.output
        assert "core.admin" in result.output
        assert "Group mappings: 1" in result.output

    def test_mapping_list_normalizes_role_key(self):
        """Some API shapes nest the role under internal_role_key — accept it."""
        mappings = [
            {"id": "m-1", "external_group_id": "grp:eng",
             "internal_role_key": "core.admin"},
        ]
        with patch("cli.commands.admin.api_get", return_value=_resp(200, mappings)):
            result = runner.invoke(app, ["admin", "mapping", "list"])
        assert result.exit_code == 0
        assert "core.admin" in result.output

    def test_mapping_list_empty(self):
        with patch("cli.commands.admin.api_get", return_value=_resp(200, [])):
            result = runner.invoke(app, ["admin", "mapping", "list"])
        assert result.exit_code == 0
        assert "No group mappings" in result.output

    def test_mapping_create_posts_correct_body(self):
        captured = {}

        def fake_post(path, json=None, **kwargs):
            captured["path"] = path
            captured["json"] = json
            return _resp(201, {
                "id": "m-new", "external_group_id": "grp:eng",
                "role_key": "core.admin",
            })

        with patch("cli.commands.admin.api_post", side_effect=fake_post):
            result = runner.invoke(app, [
                "admin", "mapping", "create", "grp:eng", "core.admin",
            ])
        assert result.exit_code == 0, result.output
        assert captured["path"] == "/api/admin/group-mappings"
        assert captured["json"] == {
            "external_group_id": "grp:eng", "role_key": "core.admin",
        }
        assert "Created mapping" in result.output
        assert "m-new" in result.output

    def test_mapping_create_failure_reports_detail(self):
        with patch(
            "cli.commands.admin.api_post",
            return_value=_resp(404, {"detail": "Unknown role: bad.key"}),
        ):
            result = runner.invoke(app, [
                "admin", "mapping", "create", "grp:eng", "bad.key",
            ])
        assert result.exit_code == 1
        assert "Unknown role" in result.output

    def test_mapping_delete_calls_correct_path(self):
        captured = {}

        def fake_delete(path, **kwargs):
            captured["path"] = path
            return _resp(204)

        with patch("cli.commands.admin.api_delete", side_effect=fake_delete):
            result = runner.invoke(app, ["admin", "mapping", "delete", "m-1"])
        assert result.exit_code == 0
        assert captured["path"] == "/api/admin/group-mappings/m-1"
        assert "Deleted mapping m-1" in result.output

    def test_mapping_delete_not_found(self):
        with patch(
            "cli.commands.admin.api_delete",
            return_value=_resp(404, {"detail": "Mapping not found"}),
        ):
            result = runner.invoke(app, ["admin", "mapping", "delete", "missing-id"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_mapping_full_roundtrip_uses_returned_id(self):
        """Compose three calls: create returns id → list shows it → delete clears it."""
        mapping = {
            "id": "m-roundtrip", "external_group_id": "grp:devs",
            "role_key": "core.admin", "assigned_by": "admin@x.com",
        }
        with patch("cli.commands.admin.api_post", return_value=_resp(201, mapping)):
            r_create = runner.invoke(app, [
                "admin", "mapping", "create", "grp:devs", "core.admin", "--json",
            ])
        assert r_create.exit_code == 0
        created = json.loads(r_create.output)
        assert created["id"] == "m-roundtrip"

        with patch("cli.commands.admin.api_get", return_value=_resp(200, [mapping])):
            r_list = runner.invoke(app, ["admin", "mapping", "list", "--json"])
        assert r_list.exit_code == 0
        listed = json.loads(r_list.output)
        assert any(m["id"] == "m-roundtrip" for m in listed)

        with patch("cli.commands.admin.api_delete", return_value=_resp(204)):
            r_del = runner.invoke(app, ["admin", "mapping", "delete", "m-roundtrip"])
        assert r_del.exit_code == 0
        assert "Deleted mapping m-roundtrip" in r_del.output


# ---- grant-role / revoke-role / effective-roles ----


def _users_lookup(email: str, uid: str):
    """Build the response that backs _resolve_user_id(email)."""
    return _resp(200, [{"id": uid, "email": email, "name": "X"}])


class TestGrantRevokeEffectiveRoles:
    def test_grant_role_resolves_email_then_posts_grant(self):
        """email → /api/users (GET) → POST /api/admin/users/{id}/role-grants."""
        captured_get = []
        captured_post = {}

        def fake_get(path, **kwargs):
            captured_get.append(path)
            if path == "/api/users":
                return _users_lookup("alice@x.com", "uid-alice")
            return _resp(404, text="Unexpected GET")

        def fake_post(path, json=None, **kwargs):
            captured_post["path"] = path
            captured_post["json"] = json
            return _resp(201, {
                "id": "grant-1", "user_id": "uid-alice",
                "role_key": "core.admin",
            })

        with patch("cli.commands.admin.api_get", side_effect=fake_get), \
             patch("cli.commands.admin.api_post", side_effect=fake_post):
            result = runner.invoke(app, [
                "admin", "grant-role", "alice@x.com", "core.admin",
            ])
        assert result.exit_code == 0, result.output
        assert "/api/users" in captured_get
        assert captured_post["path"] == "/api/admin/users/uid-alice/role-grants"
        assert captured_post["json"] == {"role_key": "core.admin"}
        assert "Granted core.admin to alice@x.com" in result.output

    def test_grant_role_unknown_email_exits_nonzero(self):
        with patch(
            "cli.commands.admin.api_get",
            return_value=_resp(200, [{"id": "u1", "email": "bob@x.com"}]),
        ):
            result = runner.invoke(app, [
                "admin", "grant-role", "alice@x.com", "core.admin",
            ])
        assert result.exit_code == 1
        assert "User not found" in result.output

    def test_revoke_role_finds_grant_then_deletes(self):
        captured_delete = {}

        def fake_get(path, **kwargs):
            if path == "/api/users":
                return _users_lookup("alice@x.com", "uid-alice")
            if path.endswith("/role-grants"):
                return _resp(200, [
                    {"id": "g-other", "role_key": "core.viewer"},
                    {"id": "g-target", "role_key": "core.admin"},
                ])
            return _resp(404, text="Unexpected GET")

        def fake_delete(path, **kwargs):
            captured_delete["path"] = path
            return _resp(204)

        with patch("cli.commands.admin.api_get", side_effect=fake_get), \
             patch("cli.commands.admin.api_delete", side_effect=fake_delete):
            result = runner.invoke(app, [
                "admin", "revoke-role", "alice@x.com", "core.admin",
            ])
        assert result.exit_code == 0, result.output
        assert captured_delete["path"] == \
            "/api/admin/users/uid-alice/role-grants/g-target"
        assert "Revoked core.admin from alice@x.com" in result.output

    def test_revoke_role_no_matching_grant_errors(self):
        def fake_get(path, **kwargs):
            if path == "/api/users":
                return _users_lookup("alice@x.com", "uid-alice")
            if path.endswith("/role-grants"):
                return _resp(200, [{"id": "g-1", "role_key": "core.viewer"}])
            return _resp(404, text="Unexpected GET")

        with patch("cli.commands.admin.api_get", side_effect=fake_get):
            result = runner.invoke(app, [
                "admin", "revoke-role", "alice@x.com", "core.admin",
            ])
        assert result.exit_code == 1
        assert "No active grant" in result.output

    def test_revoke_role_alternate_field_internal_role_key(self):
        """API may return grants with internal_role_key instead of role_key."""
        def fake_get(path, **kwargs):
            if path == "/api/users":
                return _users_lookup("alice@x.com", "uid-alice")
            if path.endswith("/role-grants"):
                return _resp(200, [
                    {"id": "g-1", "internal_role_key": "core.admin"},
                ])
            return _resp(404, text="Unexpected GET")

        with patch("cli.commands.admin.api_get", side_effect=fake_get), \
             patch("cli.commands.admin.api_delete", return_value=_resp(204)):
            result = runner.invoke(app, [
                "admin", "revoke-role", "alice@x.com", "core.admin",
            ])
        assert result.exit_code == 0, result.output
        assert "Revoked core.admin" in result.output

    def test_effective_roles_pretty_print(self):
        def fake_get(path, **kwargs):
            if path == "/api/users":
                return _users_lookup("alice@x.com", "uid-alice")
            if path.endswith("/effective-roles"):
                return _resp(200, {
                    "direct": ["core.analyst"],
                    "group": ["core.km_admin"],
                    "expanded": ["core.km_admin", "core.analyst", "core.viewer"],
                })
            return _resp(404, text="Unexpected GET")

        with patch("cli.commands.admin.api_get", side_effect=fake_get):
            result = runner.invoke(app, [
                "admin", "effective-roles", "alice@x.com",
            ])
        assert result.exit_code == 0, result.output
        assert "alice@x.com" in result.output
        assert "core.analyst" in result.output
        assert "core.km_admin" in result.output
        assert "core.viewer" in result.output

    def test_effective_roles_alternate_field_names(self):
        """Tolerate API returning direct_roles/group_roles/effective_roles."""
        def fake_get(path, **kwargs):
            if path == "/api/users":
                return _users_lookup("alice@x.com", "uid-alice")
            if path.endswith("/effective-roles"):
                return _resp(200, {
                    "direct_roles": ["core.admin"],
                    "group_roles": [],
                    "effective_roles": ["core.admin", "core.km_admin",
                                        "core.analyst", "core.viewer"],
                })
            return _resp(404, text="Unexpected GET")

        with patch("cli.commands.admin.api_get", side_effect=fake_get):
            result = runner.invoke(app, ["admin", "effective-roles", "alice@x.com"])
        assert result.exit_code == 0, result.output
        assert "core.admin" in result.output

    def test_effective_roles_json(self):
        payload = {"direct": ["core.admin"], "group": [], "expanded": ["core.admin"]}

        def fake_get(path, **kwargs):
            if path == "/api/users":
                return _users_lookup("alice@x.com", "uid-alice")
            if path.endswith("/effective-roles"):
                return _resp(200, payload)
            return _resp(404, text="Unexpected GET")

        with patch("cli.commands.admin.api_get", side_effect=fake_get):
            result = runner.invoke(app, [
                "admin", "effective-roles", "alice@x.com", "--json",
            ])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["expanded"] == ["core.admin"]


# ---- PAT-aware integration test (auto-skips until sibling API lands) ----


def _role_management_router_present() -> bool:
    """True iff the FastAPI app has the /api/admin/internal-roles route.

    Skips PAT integration tests cleanly when this CLI change lands ahead of
    the sibling agent's app/api/role_management.py commit.
    """
    try:
        from app.main import app as fastapi_app
        for route in fastapi_app.router.routes:
            path = getattr(route, "path", "")
            if path == "/api/admin/internal-roles":
                return True
        return False
    except Exception:
        return False


@pytest.mark.skipif(
    not _role_management_router_present(),
    reason="role_management API not yet registered",
)
def test_role_list_over_pat_integration(monkeypatch, tmp_path):
    """End-to-end: a PAT-authenticated admin can hit /api/admin/internal-roles.

    Runs the CLI machinery (api_get) against a TestClient so we exercise the
    PAT verification path the contract specifically asks us to validate.
    """
    import hashlib
    import os
    import uuid
    from datetime import datetime, timezone, timedelta

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")

    from src.db import get_system_db, close_system_db
    from src.repositories.users import UserRepository
    from src.repositories.access_tokens import AccessTokenRepository
    from app.auth.jwt import create_access_token
    from app.main import app as fastapi_app
    from fastapi.testclient import TestClient

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@t", name="A", role="admin")
        tid = str(uuid.uuid4())
        pat = create_access_token(
            user_id=uid, email="admin@t", role="admin",
            token_id=tid, typ="pat",
            expires_delta=timedelta(days=30),
        )
        AccessTokenRepository(conn).create(
            id=tid, user_id=uid, name="ci-pat",
            token_hash=hashlib.sha256(pat.encode()).hexdigest(),
            prefix=tid.replace("-", "")[:8],
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
    finally:
        conn.close()
        close_system_db()

    client = TestClient(fastapi_app)
    resp = client.get(
        "/api/admin/internal-roles",
        headers={"Authorization": f"Bearer {pat}"},
    )
    # 200 = PAT resolved core.admin via auto-seeded user_role_grants;
    # 404 here would mean route registration drift between this CLI test
    # and the API agent's contract — fail loud.
    assert resp.status_code == 200, resp.text
