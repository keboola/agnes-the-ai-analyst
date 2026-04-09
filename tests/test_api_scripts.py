"""Tests for scripts and settings API endpoints."""

import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setenv("SCRIPT_TIMEOUT", "10")

    from app.main import create_app
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.sync_settings import DatasetPermissionRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    user_repo = UserRepository(conn)
    user_repo.create(id="admin1", email="admin@acme.com", name="Admin", role="admin")
    user_repo.create(id="analyst1", email="analyst@acme.com", name="Analyst", role="analyst")

    perm_repo = DatasetPermissionRepository(conn)
    perm_repo.grant("analyst1", "sales", "read")
    perm_repo.grant("analyst1", "support", "read")
    conn.close()

    app = create_app()
    test_client = TestClient(app)
    admin_token = create_access_token("admin1", "admin@acme.com", "admin")
    analyst_token = create_access_token("analyst1", "analyst@acme.com", "analyst")

    return test_client, admin_token, analyst_token


class TestScriptsAPI:
    def test_list_scripts_empty(self, client):
        c, _, analyst_token = client
        resp = c.get("/api/scripts", headers={"Authorization": f"Bearer {analyst_token}"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_deploy_and_list(self, client):
        c, _, analyst_token = client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        resp = c.post("/api/scripts/deploy", json={
            "name": "hello", "source": "print('hello world')",
        }, headers=headers)
        assert resp.status_code == 201
        script_id = resp.json()["id"]

        resp = c.get("/api/scripts", headers=headers)
        assert resp.json()["count"] == 1

    def test_run_script(self, client):
        c, _, analyst_token = client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        resp = c.post("/api/scripts/run", json={
            "source": "print('hello from script')", "name": "test",
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == 0
        assert "hello from script" in data["stdout"]

    def test_run_blocked_import(self, client):
        c, _, analyst_token = client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        resp = c.post("/api/scripts/run", json={
            "source": "import subprocess; subprocess.run(['ls'])", "name": "bad",
        }, headers=headers)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "disallowed" in detail or "Blocked" in detail

    def test_deploy_run_undeploy(self, client):
        c, _, analyst_token = client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        # Deploy
        resp = c.post("/api/scripts/deploy", json={
            "name": "calc", "source": "print(2+2)", "schedule": "0 8 * * MON",
        }, headers=headers)
        script_id = resp.json()["id"]

        # Run
        resp = c.post(f"/api/scripts/{script_id}/run", headers=headers)
        assert resp.status_code == 200
        assert "4" in resp.json()["stdout"]

        # Undeploy
        resp = c.delete(f"/api/scripts/{script_id}", headers=headers)
        assert resp.status_code == 204


class TestSettingsAPI:
    def test_get_settings(self, client):
        c, _, analyst_token = client
        resp = c.get("/api/settings", headers={"Authorization": f"Bearer {analyst_token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "analyst1"
        assert len(data["permissions"]) == 2

    def test_enable_dataset(self, client):
        c, _, analyst_token = client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        resp = c.put("/api/settings/dataset", json={
            "dataset": "sales", "enabled": True,
        }, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    def test_enable_unauthorized_dataset(self, client):
        c, _, analyst_token = client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        resp = c.put("/api/settings/dataset", json={
            "dataset": "hr_secret", "enabled": True,
        }, headers=headers)
        assert resp.status_code == 403
