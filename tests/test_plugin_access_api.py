"""API tests for /api/user-groups — system-group protection."""

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestSystemGroupProtection:
    """System groups Admin/Everyone (seeded at startup) must reject update/delete."""

    def _system_group_id(self, c, token, name):
        resp = c.get("/api/user-groups", headers=_auth(token))
        assert resp.status_code == 200
        for g in resp.json():
            if g["name"] == name:
                return g["id"]
        pytest.fail(f"system group {name!r} was not seeded")

    def test_admin_group_is_seeded(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/api/user-groups", headers=_auth(token))
        assert resp.status_code == 200
        names = {g["name"]: g for g in resp.json()}
        assert "Admin" in names
        assert names["Admin"]["is_system"] is True
        assert "Everyone" in names
        assert names["Everyone"]["is_system"] is True

    def test_update_system_group_returns_403(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        gid = self._system_group_id(c, token, "Admin")
        resp = c.patch(
            f"/api/user-groups/{gid}",
            json={"name": "RenamedAdmin"},
            headers=_auth(token),
        )
        assert resp.status_code == 403
        assert "system group" in resp.json()["detail"].lower()

    def test_delete_system_group_returns_403(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        gid = self._system_group_id(c, token, "Everyone")
        resp = c.delete(f"/api/user-groups/{gid}", headers=_auth(token))
        assert resp.status_code == 403

    def test_non_system_group_crud_still_works(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        created = c.post(
            "/api/user-groups",
            json={"name": "NormalGroup", "description": "users can delete this"},
            headers=_auth(token),
        )
        assert created.status_code == 201
        gid = created.json()["id"]
        assert created.json()["is_system"] is False
        patched = c.patch(
            f"/api/user-groups/{gid}",
            json={"description": "renamed"},
            headers=_auth(token),
        )
        assert patched.status_code == 200
        deleted = c.delete(f"/api/user-groups/{gid}", headers=_auth(token))
        assert deleted.status_code == 204
