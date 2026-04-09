"""Tests for FastAPI endpoints."""

import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    from app.main import create_app
    app = create_app()
    return TestClient(app)


@pytest.fixture
def seeded_client(tmp_path, monkeypatch):
    """Client with a pre-created admin user and JWT token."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    from app.main import create_app
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    conn = get_system_db()
    repo = UserRepository(conn)
    repo.create(id="admin1", email="admin@acme.com", name="Admin", role="admin")
    repo.create(id="analyst1", email="analyst@acme.com", name="Analyst", role="analyst")
    conn.close()

    app = create_app()
    client = TestClient(app)

    admin_token = create_access_token("admin1", "admin@acme.com", "admin")
    analyst_token = create_access_token("analyst1", "analyst@acme.com", "analyst")

    return client, admin_token, analyst_token


# ---- Health ----

class TestHealth:
    def test_health_no_auth(self, app_client):
        resp = app_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("healthy", "degraded", "unhealthy")
        assert "services" in data

    def test_health_has_duckdb_check(self, app_client):
        resp = app_client.get("/api/health")
        data = resp.json()
        assert "duckdb_state" in data["services"]
        assert data["services"]["duckdb_state"]["status"] == "ok"


# ---- Auth ----

class TestAuth:
    def test_token_for_existing_user(self, seeded_client):
        client, _, _ = seeded_client
        resp = client.post("/auth/token", json={"email": "admin@acme.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["role"] == "admin"

    def test_token_for_unknown_user(self, seeded_client):
        client, _, _ = seeded_client
        resp = client.post("/auth/token", json={"email": "nobody@acme.com"})
        assert resp.status_code == 401

    def test_protected_endpoint_without_token(self, seeded_client):
        client, _, _ = seeded_client
        resp = client.get("/api/users")
        assert resp.status_code == 401

    def test_protected_endpoint_with_token(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.get("/api/users", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200


# ---- RBAC ----

class TestRBAC:
    def test_admin_can_list_users(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.get("/api/users", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_analyst_cannot_list_users(self, seeded_client):
        client, _, analyst_token = seeded_client
        resp = client.get("/api/users", headers={"Authorization": f"Bearer {analyst_token}"})
        assert resp.status_code == 403

    def test_analyst_cannot_trigger_sync(self, seeded_client):
        client, _, analyst_token = seeded_client
        resp = client.post("/api/sync/trigger", headers={"Authorization": f"Bearer {analyst_token}"})
        assert resp.status_code == 403


# ---- Sync Manifest ----

class TestSyncManifest:
    def test_manifest_returns_tables(self, seeded_client):
        client, admin_token, _ = seeded_client
        # Seed some sync state
        from src.db import get_system_db
        from src.repositories.sync_state import SyncStateRepository
        conn = get_system_db()
        repo = SyncStateRepository(conn)
        repo.update_sync(table_id="orders", rows=1000, file_size_bytes=5000, hash="abc")
        conn.close()

        resp = client.get("/api/sync/manifest", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert "tables" in data
        assert "orders" in data["tables"]
        assert data["tables"]["orders"]["rows"] == 1000


# ---- Users CRUD ----

class TestUsersCRUD:
    def test_create_user(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/users",
            json={"email": "new@acme.com", "name": "New User"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 201
        assert resp.json()["email"] == "new@acme.com"

    def test_create_duplicate_user(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.post(
            "/api/users",
            json={"email": "admin@acme.com", "name": "Duplicate"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 409

    def test_delete_user(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.delete(
            "/api/users/analyst1",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 204


# ---- Knowledge / Memory ----

class TestMemory:
    def test_create_and_list(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        # Create
        resp = client.post("/api/memory", json={
            "title": "MRR Definition",
            "content": "Monthly recurring revenue",
            "category": "metrics",
        }, headers=headers)
        assert resp.status_code == 201
        item_id = resp.json()["id"]

        # List
        resp = client.get("/api/memory", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    def test_vote(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        resp = client.post("/api/memory", json={
            "title": "Test", "content": "test", "category": "test",
        }, headers=headers)
        item_id = resp.json()["id"]

        resp = client.post(f"/api/memory/{item_id}/vote", json={"vote": 1}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["upvotes"] == 1

    def test_search(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}

        client.post("/api/memory", json={
            "title": "Revenue report", "content": "MRR trends", "category": "finance",
        }, headers=headers)
        client.post("/api/memory", json={
            "title": "Support SLA", "content": "Response times", "category": "support",
        }, headers=headers)

        resp = client.get("/api/memory?search=revenue", headers=headers)
        assert resp.json()["count"] == 1


# ---- Upload ----

class TestUpload:
    def test_upload_session(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}
        resp = client.post(
            "/api/upload/sessions",
            files={"file": ("session.jsonl", b'{"role":"user","content":"hello"}', "application/jsonl")},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["size"] > 0

    def test_upload_local_md(self, seeded_client):
        client, _, analyst_token = seeded_client
        headers = {"Authorization": f"Bearer {analyst_token}"}
        resp = client.post(
            "/api/upload/local-md",
            json={"content": "# My knowledge\n\nMRR = Monthly Recurring Revenue"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["user"] == "analyst@acme.com"
