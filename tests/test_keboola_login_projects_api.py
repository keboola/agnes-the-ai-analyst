"""The select-mode project surface: GET /api/auth/keboola/projects and
POST /api/auth/keboola/projects (session-user auth, own stash only).
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.auth import keboola_provisioning as kprov
from app.auth.providers import keboola_projects as kp
from app.auth.providers import keboola_verify as kv
from connectors.keboola.storage_api import KeboolaStorageClient

BASE = "/api/auth/keboola/projects"
STACK = "https://connection.example.com"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def select_env(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    from app.secrets_vault import _reset_ephemeral_key_for_tests

    _reset_ephemeral_key_for_tests()
    monkeypatch.setattr(kv, "stack_url", lambda: STACK)
    monkeypatch.setattr(kv, "multi_project_mode", lambda: "select")
    monkeypatch.setattr(kp, "exchange_project_pat", lambda tok, pid, *, read_only: f"pat-{pid}")
    monkeypatch.setattr(
        KeboolaStorageClient,
        "verify_token",
        lambda self: {
            "isMasterToken": False,
            "owner": {"id": int(self.token.split("-", 1)[1]), "name": "P"},
        },
    )
    return seeded_app


class TestListDiscoveredProjects:
    def test_requires_auth(self, seeded_app):
        assert seeded_app["client"].get(BASE).status_code == 401

    def test_no_discovery_in_default_mode(self, seeded_app):
        resp = seeded_app["client"].get(BASE, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "disabled"
        assert body["discovery_available"] is False
        assert body["projects"] == []

    def test_lists_the_callers_stash_with_imported_flags(self, select_env):
        from src.repositories import users_repo

        analyst = users_repo().get_by_email("analyst@test.com")
        projects = [
            kp.DiscoveredProject(id="516", name="A", role="admin"),
            kp.DiscoveredProject(id="7", name="B", role="readOnly"),
        ]
        assert kprov.store_pending_discovery(analyst, projects, "at-1") is True
        kprov.provision_selected(analyst, ["516"])

        resp = select_env["client"].get(BASE, headers=_auth(select_env["analyst_token"]))
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "select"
        assert body["discovery_available"] is True
        flags = {p["id"]: p["imported"] for p in body["projects"]}
        assert flags == {"516": True, "7": False}

    def test_another_users_stash_is_invisible(self, select_env):
        from src.repositories import users_repo

        admin = users_repo().get_by_email("admin@test.com")
        kprov.store_pending_discovery(admin, [kp.DiscoveredProject(id="516", name="A", role="admin")], "at-1")
        resp = select_env["client"].get(BASE, headers=_auth(select_env["analyst_token"]))
        assert resp.json()["discovery_available"] is False


class TestImportDiscoveredProjects:
    def test_import_provisions_the_selection(self, select_env):
        from src.repositories import source_connections_repo, users_repo

        analyst = users_repo().get_by_email("analyst@test.com")
        kprov.store_pending_discovery(
            analyst,
            [
                kp.DiscoveredProject(id="516", name="A", role="admin"),
                kp.DiscoveredProject(id="7", name="B", role="guest"),
            ],
            "at-1",
        )
        resp = select_env["client"].post(
            BASE, json={"project_ids": ["516"]}, headers=_auth(select_env["analyst_token"])
        )
        assert resp.status_code == 200
        body = resp.json()
        assert [p["project_id"] for p in body["projects"]] == ["516"]
        assert body["projects"][0]["token_stored"] is True
        connected = {
            str((row.get("config") or {}).get("project_id"))
            for row in source_connections_repo().list(source_type="keboola")
        }
        assert "516" in connected and "7" not in connected

    def test_outside_select_mode_is_409(self, seeded_app, monkeypatch):
        monkeypatch.setattr(kv, "multi_project_mode", lambda: "auto")
        resp = seeded_app["client"].post(
            BASE, json={"project_ids": ["516"]}, headers=_auth(seeded_app["analyst_token"])
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "not_select_mode"

    def test_without_stash_is_409_discovery_expired(self, select_env):
        resp = select_env["client"].post(
            BASE, json={"project_ids": ["516"]}, headers=_auth(select_env["analyst_token"])
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "discovery_expired"

    def test_unknown_project_is_400(self, select_env):
        from src.repositories import users_repo

        analyst = users_repo().get_by_email("analyst@test.com")
        kprov.store_pending_discovery(analyst, [kp.DiscoveredProject(id="516", name="A", role="admin")], "at-1")
        resp = select_env["client"].post(
            BASE, json={"project_ids": ["999"]}, headers=_auth(select_env["analyst_token"])
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "unknown_project"

    def test_empty_selection_is_400(self, select_env):
        resp = select_env["client"].post(
            BASE, json={"project_ids": []}, headers=_auth(select_env["analyst_token"])
        )
        assert resp.status_code == 400
