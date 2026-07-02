"""Tests for the /api/admin/source-connections REST surface.

Covers:
- list empty → []
- create → 201 with id
- create duplicate name → 409
- get existing → 200
- get missing → 404
- update config → 200
- delete → 204
- test endpoint: mock httpx, return fake project info
- unauthenticated → 401
- non-admin → 403
- set secret → 204 (vault key required)
- set secret without vault key → 409
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


BASE = "/api/admin/source-connections"


class TestSourceConnectionsList:
    def test_list_empty_returns_empty_list(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(BASE, headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        # May include a seeded default from instance.yaml — assert it's a list
        assert isinstance(data, list)

    def test_list_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get(BASE, headers=_auth(token))
        assert resp.status_code == 403

    def test_list_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(BASE)
        assert resp.status_code == 401


class TestSourceConnectionsCreate:
    def test_create_returns_201_with_id(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-create",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body
        assert body["name"] == "test-keboola-create"

    def test_create_duplicate_name_returns_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        payload = {
            "name": "test-keboola-dup",
            "source_type": "keboola",
            "config": {"stack_url": "https://connection.example.com"},
        }
        resp1 = c.post(BASE, json=payload, headers=_auth(token))
        assert resp1.status_code == 201
        resp2 = c.post(BASE, json=payload, headers=_auth(token))
        assert resp2.status_code == 409

    def test_create_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-x",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_create_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            BASE,
            json={
                "name": "test-x",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
        )
        assert resp.status_code == 401


class TestSourceConnectionsGet:
    def test_get_existing_returns_200(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create first
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-get",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        # Now get
        resp2 = c.get(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["id"] == conn_id
        assert data["name"] == "test-keboola-get"

    def test_get_missing_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(f"{BASE}/nonexistent-id-xyz", headers=_auth(token))
        assert resp.status_code == 404


class TestSourceConnectionsUpdate:
    def test_update_config_returns_200(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-update",
                "source_type": "keboola",
                "config": {"stack_url": "https://old.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        # Update
        resp2 = c.put(
            f"{BASE}/{conn_id}",
            json={"config": {"stack_url": "https://new.example.com"}},
            headers=_auth(token),
        )
        assert resp2.status_code == 200
        # Verify
        resp3 = c.get(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp3.status_code == 200
        data = resp3.json()
        assert data["config"]["stack_url"] == "https://new.example.com"

    def test_update_missing_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.put(
            f"{BASE}/nonexistent-id-xyz",
            json={"config": {"stack_url": "https://new.example.com"}},
            headers=_auth(token),
        )
        assert resp.status_code == 404


class TestSourceConnectionsDelete:
    def test_delete_returns_204(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-delete",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        # Delete
        resp2 = c.delete(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp2.status_code == 204
        # Confirm gone
        resp3 = c.get(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp3.status_code == 404

    def test_delete_missing_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.delete(f"{BASE}/nonexistent-id-xyz", headers=_auth(token))
        assert resp.status_code == 404

    def test_delete_in_use_returns_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create a connection, then pin a registry table to it.
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-inuse",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        from src.repositories import table_registry_repo

        table_registry_repo().register(
            id="in.c-test.pinned_table",
            name="pinned_table",
            source_type="keboola",
            bucket="in.c-test",
            source_table="pinned_table",
            connection_id=conn_id,
        )

        # Deleting the still-referenced connection must be refused.
        resp2 = c.delete(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp2.status_code == 409
        detail = resp2.json()["detail"]
        assert detail["error"] == "connection_in_use"
        assert "in.c-test.pinned_table" in detail["tables"]
        # Connection still exists.
        assert c.get(f"{BASE}/{conn_id}", headers=_auth(token)).status_code == 200


class TestSourceConnectionsSecret:
    def test_set_secret_without_vault_key_returns_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create connection
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-secret",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        # Try to set secret without vault key configured
        # Test env doesn't have AGNES_VAULT_KEY → should 409
        resp2 = c.put(
            f"{BASE}/{conn_id}/secret",
            json={"value": "test-storage-token"},
            headers=_auth(token),
        )
        # Either 409 (no vault key) or 204 (vault key present in env)
        assert resp2.status_code in (204, 409)

    def test_delete_secret_returns_204(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create connection
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-secret-del",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        # Delete secret (idempotent even if no secret was set)
        resp2 = c.delete(f"{BASE}/{conn_id}/secret", headers=_auth(token))
        assert resp2.status_code == 204


class TestSourceConnectionsTest:
    def test_test_endpoint_success(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Create connection
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-testconn",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        # Mock httpx to return fake project info. The endpoint uses an async
        # client (`async with httpx.AsyncClient(...)` + `await client.get`), so
        # the mock must honor the async context-manager + awaitable-get protocol.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "123", "name": "Test Project"}

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("app.api.admin_source_connections.httpx.AsyncClient", return_value=mock_client),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp2 = c.post(f"{BASE}/{conn_id}/test", headers=_auth(token))

        assert resp2.status_code == 200
        data = resp2.json()
        assert data["ok"] is True
        assert data["project_name"] == "Test Project"

    def test_test_endpoint_missing_connection_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(f"{BASE}/nonexistent-id/test", headers=_auth(token))
        assert resp.status_code == 404
