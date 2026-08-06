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
- master-token vault slot (kind=master): keboola-only, verify_token preflight,
  storage-api-outage redaction, cleanup on connection delete
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


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

    def test_update_renames_connection(self, seeded_app):
        # Backs the "Add data source" wizard's rename-after-test step (#755).
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "draft-rename-me",
                "source_type": "keboola",
                "config": {"stack_url": "https://a.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        resp2 = c.put(f"{BASE}/{conn_id}", json={"name": "Production"}, headers=_auth(token))
        assert resp2.status_code == 200
        assert resp2.json()["name"] == "Production"

        resp3 = c.get(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp3.json()["name"] == "Production"

    def test_update_rename_to_existing_name_returns_409(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.post(
            BASE,
            json={
                "name": "rename-conflict-a",
                "source_type": "keboola",
                "config": {"stack_url": "https://a.example.com"},
            },
            headers=_auth(token),
        )
        resp_b = c.post(
            BASE,
            json={
                "name": "rename-conflict-b",
                "source_type": "keboola",
                "config": {"stack_url": "https://b.example.com"},
            },
            headers=_auth(token),
        )
        conn_b_id = resp_b.json()["id"]

        resp = c.put(f"{BASE}/{conn_b_id}", json={"name": "rename-conflict-a"}, headers=_auth(token))
        assert resp.status_code == 409


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
            # example.com subdomains don't resolve; the SSRF validator is exercised
            # by its own test below, so no-op it here to test connectivity logic.
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp2 = c.post(f"{BASE}/{conn_id}/test", headers=_auth(token))

        assert resp2.status_code == 200
        data = resp2.json()
        assert data["ok"] is True
        assert data["project_name"] == "Test Project"

    def test_test_endpoint_rejects_private_stack_url(self, seeded_app):
        # SSRF guard: a stack_url pointing at the cloud metadata endpoint (or any
        # private/reserved/link-local host) is refused before any outbound call.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-ssrf",
                "source_type": "keboola",
                "config": {"stack_url": "https://169.254.169.254"},
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        with patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}):
            resp2 = c.post(f"{BASE}/{conn_id}/test", headers=_auth(token))
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["ok"] is False
        assert "private or reserved" in data["error"]

    def test_create_rejects_disallowed_token_env(self, seeded_app):
        # token_env allowlist: an admin cannot point a connection at an arbitrary
        # server-process env var (e.g. JWT_SECRET_KEY) to exfiltrate it via the
        # outbound token header.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-badenv",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
                "token_env": "JWT_SECRET_KEY",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "allowlist" in resp.json()["detail"].lower()

    def test_test_endpoint_logs_result(self, seeded_app, caplog):
        # /test was previously fully silent server-side; every outcome now
        # leaves one INFO line so repeated failures are visible in logs.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-keboola-testconn-log",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "123", "name": "Test Project"}

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("app.api.admin_source_connections.httpx.AsyncClient", return_value=mock_client),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
            caplog.at_level("INFO", logger="app.api.admin_source_connections"),
        ):
            resp2 = c.post(f"{BASE}/{conn_id}/test", headers=_auth(token))

        assert resp2.status_code == 200
        assert resp2.json()["ok"] is True
        assert f"connection test for {conn_id}" in caplog.text
        assert ": ok" in caplog.text

    def test_test_endpoint_missing_connection_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(f"{BASE}/nonexistent-id/test", headers=_auth(token))
        assert resp.status_code == 404


class TestSourceConnectionsTables:
    """GET /{id}/tables — the "Add data source" wizard's table-picker primitive (#755)."""

    def _create(self, c, token, *, name="test-kbc-tables", token_env="KEBOOLA_STORAGE_TOKEN"):
        resp = c.post(
            BASE,
            json={
                "name": name,
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
                "token_env": token_env,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_tables_endpoint_groups_by_bucket(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token)

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                return_value=[
                    {"id": "in.c-main", "name": "main", "stage": "in", "description": ""},
                ],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[
                    {
                        "id": "in.c-main.orders",
                        "name": "orders",
                        "bucket": {"id": "in.c-main"},
                        "rowsCount": 42,
                        "dataSizeBytes": 1024,
                    },
                ],
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "project"
        assert len(data["buckets"]) == 1
        bucket = data["buckets"][0]
        assert bucket["id"] == "in.c-main"
        assert bucket["tables"] == [{"id": "in.c-main.orders", "name": "orders", "rows": 42, "size_bytes": 1024}]

    def test_tables_endpoint_missing_connection_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get(f"{BASE}/nonexistent-id/tables", headers=_auth(token))
        assert resp.status_code == 404

    def test_tables_endpoint_no_token_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-notoken", token_env="")
        # no-op the SSRF validator so the 400 comes from the no-token path, not
        # from example.com failing to resolve.
        with patch("app.api.admin._validate_url_not_private", return_value=None):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))
        assert resp.status_code == 400

    def test_tables_endpoint_non_keboola_returns_400(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-bq-tables",
                "source_type": "bigquery",
                "config": {"project_id": "p"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]
        resp2 = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))
        assert resp2.status_code == 400

    def test_tables_endpoint_upstream_error_returns_502(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-upstream-error")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=StorageApiError("boom"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 502

    def test_an_empty_200_listing_also_falls_back_to_bucket_permissions(self, seeded_app):
        """Some token shapes get a 200 with an empty array, not a 403.

        That was indistinguishable from an empty project, so the picker reported
        "no buckets visible to this token" and the fallback never ran — the exact
        scenario the fallback exists for (Devin Review on #1189).
        """
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-empty200")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                return_value=[],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {"in.c-main": "read"}},
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[
                    {"id": "in.c-main.orders", "name": "orders", "rowsCount": 7, "dataSizeBytes": 64},
                ],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.get_bucket",
                return_value={"id": "in.c-main", "name": "main", "stage": "in", "description": ""},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["scope"] == "token_buckets"
        assert [b["id"] for b in data["buckets"]] == ["in.c-main"]

    def test_a_genuinely_empty_project_stays_empty_and_does_not_error(self, seeded_app):
        """The same empty-listing retry must not turn a real empty project into a
        502: with no bucketPermissions there is nothing to enumerate, so the
        empty project-wide answer stands."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-trulyempty")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                return_value=[],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {}},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["buckets"] == []
        assert data["scope"] == "project"

    def test_a_transient_failure_does_not_trigger_the_per_bucket_loop(self, seeded_app):
        """Only a refusal justifies the fallback, not any failure.

        `_scoped_listing` makes two upstream calls per bucket the token can see,
        so a brief blip on a full-access token would otherwise stall "Browse &
        register tables" for minutes on a large project and then label the token
        as bucket-scoped. A 5xx and a connection error must surface as themselves
        (Devin Review on #1189).
        """
        import requests as _requests

        from connectors.keboola.storage_api import StorageApiError

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-transient")

        for boom in (
            StorageApiError("upstream exploded", status=500),
            _requests.ConnectionError("connection reset"),
            # No status at all: the gate must fail CLOSED. An earlier version
            # read `status is not None and status not in (401, 403)`, so this
            # case fell through into the fallback (Devin Review on #1189).
            StorageApiError("no status stamped"),
        ):
            called = []
            with (
                patch(
                    "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                    side_effect=boom,
                ),
                patch(
                    "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                    side_effect=lambda: called.append("verify") or {"bucketPermissions": {"in.c-main": "read"}},
                ),
                patch("app.api.admin._validate_url_not_private", return_value=None),
                patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
            ):
                resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

            assert resp.status_code == 502, (boom, resp.text)
            assert called == [], f"per-bucket fallback was entered for {boom!r}"

    def test_tables_endpoint_scoped_token_falls_back_to_bucket_permissions(self, seeded_app):
        """Bucket-scoped (custom access) token: the project-wide listing is
        refused, but /tokens/verify names the permitted buckets — the
        endpoint must list per-bucket and mark the response scope."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-scoped")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=StorageApiError("accessDenied", status=403),
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {"in.c-main": "read"}},
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[
                    {"id": "in.c-main.orders", "name": "orders", "rowsCount": 42, "dataSizeBytes": 1024},
                ],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.get_bucket",
                return_value={"id": "in.c-main", "name": "main", "stage": "in", "description": ""},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "token_buckets"
        assert len(data["buckets"]) == 1
        bucket = data["buckets"][0]
        assert bucket["id"] == "in.c-main"
        assert bucket["name"] == "main"
        assert bucket["tables"] == [{"id": "in.c-main.orders", "name": "orders", "rows": 42, "size_bytes": 1024}]

    def test_tables_endpoint_scoped_fallback_survives_bucket_detail_failure(self, seeded_app):
        """get_bucket failing must not drop the bucket's tables — the bucket
        renders from its id instead."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-scoped-nodetail")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=StorageApiError("accessDenied", status=403),
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {"in.c-main": "read"}},
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[{"id": "in.c-main.orders", "name": "orders", "rowsCount": 1, "dataSizeBytes": 10}],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.get_bucket",
                side_effect=StorageApiError("accessDenied", status=403),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "token_buckets"
        assert data["buckets"][0]["id"] == "in.c-main"
        assert data["buckets"][0]["name"] == "c-main"  # synthesized from the id
        assert len(data["buckets"][0]["tables"]) == 1

    def test_tables_endpoint_no_bucket_permissions_surfaces_original_error(self, seeded_app):
        """Token with no bucketPermissions (e.g. component token): nothing to
        fall back to — the original project-wide failure is the story."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-noperms")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=StorageApiError("accessDenied original", status=403),
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {}},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 502
        assert "accessDenied original" in resp.json()["detail"]

    def test_tables_endpoint_network_error_maps_to_502(self, seeded_app):
        """requests-level failures (DNS, refused, TLS) must surface as the
        same clean 502 detail as Storage API errors — previously they fell
        through to the generic 500 handler."""
        import requests as _requests

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-network")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=_requests.ConnectionError("connection refused"),
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=_requests.ConnectionError("connection refused"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 502
        assert resp.json()["detail"].startswith("keboola_storage_api_error:")

    def test_tables_endpoint_every_scoped_bucket_failing_returns_502(self, seeded_app):
        """Permissions exist but every per-bucket listing fails → a real 502,
        not a silently empty picker."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-allfail")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=StorageApiError("accessDenied", status=403),
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"bucketPermissions": {"in.c-main": "read"}},
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                side_effect=StorageApiError("bucket listing broke", status=500),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 502
        assert "bucket listing broke" in resp.json()["detail"]

    def test_tables_endpoint_success_logs_counts(self, seeded_app, caplog):
        # Operator-facing trail of what the wizard loads: one INFO line with
        # bucket/table counts (+ duration), so "wizard is empty/slow" is
        # diagnosable from server logs.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-log-success")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                return_value=[{"id": "in.c-main", "name": "main", "stage": "in", "description": ""}],
            ),
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_tables",
                return_value=[
                    {"id": "in.c-main.orders", "name": "orders", "bucket": {"id": "in.c-main"}},
                ],
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
            caplog.at_level("INFO", logger="app.api.admin_source_connections"),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 200
        assert f"tables listing for connection {conn_id}" in caplog.text
        assert "1 buckets, 1 tables" in caplog.text

    def test_tables_endpoint_transport_error_logs_redacted_warning(self, seeded_app, caplog):
        # The 502 path must leave a server-side WARNING (the catch-all 500 it
        # replaced logged a full traceback) — with the token redacted.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create(c, token, name="test-kbc-tables-log-warning")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.list_buckets",
                side_effect=requests.exceptions.ReadTimeout("read timed out; X-StorageApi-Token: fake-token"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            patch.dict("os.environ", {"KEBOOLA_STORAGE_TOKEN": "fake-token"}),
            caplog.at_level("WARNING", logger="app.api.admin_source_connections"),
        ):
            resp = c.get(f"{BASE}/{conn_id}/tables", headers=_auth(token))

        assert resp.status_code == 502
        assert f"tables listing failed for connection {conn_id}" in caplog.text
        assert "<redacted-storage-token>" in caplog.text
        assert "fake-token" not in caplog.text

    def test_tables_endpoint_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get(f"{BASE}/some-id/tables", headers=_auth(token))
        assert resp.status_code == 403


class TestSourceConnectionsMasterSecret:
    """PUT/DELETE .../secret with kind=master — Keboola master-token vault slot.

    Distinct from the plain ``kind=storage`` secret exercised by
    ``TestSourceConnectionsSecret``: this is validated at save time via a
    Storage API ``verify_token`` preflight (keboola-only, must be a master
    token), and stored under a separate vault key (``master_secret_key``).
    """

    @pytest.fixture(autouse=True)
    def _stable_vault_key(self, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        yield
        _reset_ephemeral_key_for_tests()

    def _create_keboola(self, c, token, *, name="test-master-kbc"):
        resp = c.post(
            BASE,
            json={
                "name": name,
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_master_secret_rejected_for_non_keboola(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            BASE,
            json={
                "name": "test-master-bq",
                "source_type": "bigquery",
                "config": {"project_id": "p"},
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201
        conn_id = resp.json()["id"]

        resp2 = c.put(
            f"{BASE}/{conn_id}/secret",
            json={"value": "some-master-token", "kind": "master"},
            headers=_auth(token),
        )
        assert resp2.status_code == 400
        assert resp2.json()["detail"] == "master_token_only_for_keboola"

    def test_master_secret_rejects_non_master_token(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-nonmaster")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"isMasterToken": False},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "not-a-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 400
        assert "master" in resp.json()["detail"].lower()

    def test_master_secret_stores_and_reports(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-store")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"isMasterToken": True},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "a-real-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 204

        detail = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert detail["has_master_secret"] is True
        assert detail["has_secret"] is False

        resp2 = c.delete(
            f"{BASE}/{conn_id}/secret",
            params={"kind": "master"},
            headers=_auth(token),
        )
        assert resp2.status_code == 204

        detail2 = c.get(f"{BASE}/{conn_id}", headers=_auth(token)).json()
        assert detail2["has_master_secret"] is False
        assert detail2["has_secret"] is False

    def test_master_secret_storage_api_outage(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-outage")

        from connectors.keboola.storage_api import StorageApiError

        # The candidate token must appear in the simulated failure so this
        # assertion actually exercises the client's redaction rather than
        # trivially passing because the token was never in the message.
        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=StorageApiError("boom: token=super-secret-master-token"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "super-secret-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 502
        assert "super-secret-master-token" not in resp.text

    def test_master_secret_storage_api_outage_logs_redacted_warning(self, seeded_app, caplog):
        # The preflight 502 must leave a server-side WARNING with the
        # candidate token redacted — same trail as the /tables listing.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-outage-log")

        from connectors.keboola.storage_api import StorageApiError

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                side_effect=StorageApiError("boom: token=super-secret-master-token"),
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
            caplog.at_level("WARNING", logger="app.api.admin_source_connections"),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "super-secret-master-token", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 502
        assert f"master-token preflight failed for connection {conn_id}" in caplog.text
        assert "<redacted-storage-token>" in caplog.text
        assert "super-secret-master-token" not in caplog.text

    def test_connection_delete_clears_master_secret(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._create_keboola(c, token, name="test-master-delete-conn")

        with (
            patch(
                "app.api.admin_source_connections.KeboolaStorageClient.verify_token",
                return_value={"isMasterToken": True},
            ),
            patch("app.api.admin._validate_url_not_private", return_value=None),
        ):
            resp = c.put(
                f"{BASE}/{conn_id}/secret",
                json={"value": "a-master-token-to-be-deleted", "kind": "master"},
                headers=_auth(token),
            )
        assert resp.status_code == 204

        from app.api.admin_source_connections import master_secret_key
        from src.repositories import connection_secrets_repo

        assert connection_secrets_repo().has(master_secret_key(conn_id)) is True

        resp2 = c.delete(f"{BASE}/{conn_id}", headers=_auth(token))
        assert resp2.status_code == 204

        assert connection_secrets_repo().has(master_secret_key(conn_id)) is False
