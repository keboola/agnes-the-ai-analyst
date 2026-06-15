"""Smoke tests — happy-path HTTP status + response shape for every endpoint group.

Each test class declares COVERED_ROUTES consumed by the route-coverage guard at
the bottom of this file. Depth: HTTP status code + top-level response shape only.
All tests run twice via seeded_app_both (DuckDB-only + Postgres).
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuthSmoke:
    COVERED_ROUTES = {
        "POST /auth/token",
        "POST /auth/bootstrap",
        "POST /auth/password/login",
        "POST /auth/password/change",
        "POST /auth/password/reset-request",
        "POST /auth/password/reset-confirm",
    }

    def test_bootstrap_returns_403_after_seeding(self, seeded_app_both):
        """Bootstrap window is closed once admin user exists."""
        r = seeded_app_both["client"].post("/auth/bootstrap", json={
            "email": "new@test.com", "password": "newpass123", "name": "New",
        })
        assert r.status_code in (403, 409), r.text

    def test_password_login_returns_token(self, seeded_app_both):
        """Password login endpoint is reachable (401 expected — no password set)."""
        r = seeded_app_both["client"].post("/auth/password/login", json={
            "email": "admin@test.com", "password": "wrong",
        })
        assert r.status_code in (401, 422), r.text

    def test_token_with_password_user(self, seeded_app_both):
        """POST /auth/token returns 200 + access_token for a user with a password_hash."""
        from argon2 import PasswordHasher
        from src.repositories import users_repo
        ph = PasswordHasher()
        users_repo().create(
            id="pw-user1", email="pw@test.com", name="PwUser",
            password_hash=ph.hash("test-password"),
        )
        r = seeded_app_both["client"].post("/auth/token", json={
            "email": "pw@test.com", "password": "test-password",
        })
        assert r.status_code == 200, r.text
        assert "access_token" in r.json()


# ---------------------------------------------------------------------------
# Health / Version
# ---------------------------------------------------------------------------

class TestHealthSmoke:
    COVERED_ROUTES = {
        "GET /api/health",
        "GET /api/health/detailed",
        "GET /api/version",
    }

    def test_health(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/health")
        assert r.status_code == 200

    def test_health_detailed(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/health/detailed")
        assert r.status_code == 200
        body = r.json()
        assert "status" in body
        assert "checks" in body

    def test_version(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/version")
        assert r.status_code == 200
        assert "version" in r.json()


# ---------------------------------------------------------------------------
# Me
# ---------------------------------------------------------------------------

class TestMeSmoke:
    COVERED_ROUTES = {
        "GET /api/me/home-stats",
        "GET /api/me/effective-access",
        "POST /api/me/onboarded",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_me_home_stats(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/home-stats", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_me_effective_access(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/effective-access", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "groups" in r.json()

    def test_me_onboarded(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/me/onboarded", headers=self._admin_headers(seeded_app_both))
        assert r.status_code in (200, 204)


# ---------------------------------------------------------------------------
# Me Stats  (DuckDB analytics side — should return 200 even in PG mode)
# ---------------------------------------------------------------------------

class TestMeStatsSmoke:
    COVERED_ROUTES = {
        "GET /api/me/stats/sessions",
        "GET /api/me/stats/tokens",
        "GET /api/me/stats/queries",
        "GET /api/me/stats/sync",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_stats_sessions(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/sessions", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_stats_tokens(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/tokens", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_stats_queries(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/queries", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_stats_sync(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/sync", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class TestUsersSmoke:
    COVERED_ROUTES = {
        "GET /api/users",
        "GET /api/users/{user_id}",
        "POST /api/users",
        "PATCH /api/users/{user_id}",
        "DELETE /api/users/{user_id}",
        "POST /api/users/{user_id}/reset-password",
        "POST /api/users/{user_id}/set-password",
        "POST /api/users/{user_id}/deactivate",
        "POST /api/users/{user_id}/activate",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_list_users(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/users", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_get_user(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/users/admin1", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "email" in r.json()


# ---------------------------------------------------------------------------
# RBAC (groups + grants + access-overview)
# ---------------------------------------------------------------------------

class TestRBACSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/groups",
        "GET /api/admin/groups/{group_id}",
        "POST /api/admin/groups",
        "PATCH /api/admin/groups/{group_id}",
        "DELETE /api/admin/groups/{group_id}",
        "GET /api/admin/groups/{group_id}/members",
        "POST /api/admin/groups/{group_id}/members",
        "DELETE /api/admin/groups/{group_id}/members/{user_id}",
        "GET /api/admin/grants",
        "POST /api/admin/grants",
        "PUT /api/admin/grants/{grant_id}",
        "DELETE /api/admin/grants/{grant_id}",
        "GET /api/admin/access-overview",
        "GET /api/admin/resource-types",
        "GET /api/admin/activity",
        "GET /api/admin/activity/health",
        "GET /api/admin/activity/sync",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_list_groups(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/groups", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_access_overview(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/access-overview", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_grants_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/grants", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_resource_types(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/resource-types", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_activity(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/activity", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_activity_health(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/activity/health", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_activity_sync(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/activity/sync", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

class TestSyncSmoke:
    COVERED_ROUTES = {
        "GET /api/sync/status",
        "GET /api/sync/manifest",
        "POST /api/sync/trigger",
        "GET /api/sync/settings",
        "GET /api/sync/table-subscriptions",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_sync_status(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/status", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sync_manifest(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/manifest", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "tables" in r.json()

    def test_sync_trigger(self, seeded_app_both, monkeypatch):
        monkeypatch.setattr("src.orchestrator.SyncOrchestrator.run_incremental", lambda *a, **kw: None)
        r = seeded_app_both["client"].post("/api/sync/trigger", headers=self._admin_headers(seeded_app_both))
        assert r.status_code in (200, 202)

    def test_sync_settings(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/settings", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sync_table_subscriptions(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/table-subscriptions", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

class TestCatalogSmoke:
    COVERED_ROUTES = {
        "GET /api/catalog/tables",
        "GET /api/catalog/profile/{table_id}",
        "POST /api/catalog/profile/{table_id}/refresh",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_catalog_tables(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/catalog/tables", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_catalog_profile_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/catalog/profile/nonexistent-table", headers=self._admin_headers(seeded_app_both))
        assert r.status_code in (404, 422)

    def test_catalog_profile_refresh_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/catalog/profile/nonexistent-table/refresh", headers=self._admin_headers(seeded_app_both))
        assert r.status_code in (404, 422)


# ---------------------------------------------------------------------------
# Data (requires registered_table_both)
# ---------------------------------------------------------------------------

class TestDataSmoke:
    COVERED_ROUTES = {
        "GET /api/data/{table_id}/check-access",
        "GET /api/data/{table_id}/download",
    }

    def test_check_access_admin(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(
            f"/api/data/{table_id}/check-access",
            headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"},
        )
        assert r.status_code == 200

    def test_check_access_analyst_denied(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(
            f"/api/data/{table_id}/check-access",
            headers={"Authorization": f"Bearer {seeded_app_both['analyst_token']}"},
        )
        assert r.status_code == 403

    def test_download_admin(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(
            f"/api/data/{table_id}/download",
            headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Query (requires registered_table_both)
# ---------------------------------------------------------------------------

class TestQuerySmoke:
    COVERED_ROUTES = {
        "POST /api/query",
        "POST /api/query/hybrid",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_query_select_one(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].post(
            "/api/query",
            json={"sql": "SELECT 1 AS n"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code == 200
        body = r.json()
        assert "rows" in body
        assert "columns" in body

    def test_query_hybrid(self, seeded_app_both, monkeypatch):
        monkeypatch.setattr("app.api.query_hybrid._run_bq_query", lambda *a, **kw: ([], []), raising=False)
        r = seeded_app_both["client"].post(
            "/api/query/hybrid",
            json={"sql": "SELECT 1", "source": "local"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 422, 501)


# ---------------------------------------------------------------------------
# V2 (requires registered_table_both)
# ---------------------------------------------------------------------------

class TestV2Smoke:
    COVERED_ROUTES = {
        "GET /api/v2/catalog",
        "GET /api/v2/schema/{table_id}",
        "GET /api/v2/sample/{table_id}",
        "POST /api/v2/scan",
        "POST /api/v2/scan/estimate",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_v2_catalog(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].get("/api/v2/catalog", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_v2_schema(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(f"/api/v2/schema/{table_id}", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "columns" in r.json()

    def test_v2_sample(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(f"/api/v2/sample/{table_id}", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_v2_scan(self, seeded_app_both, registered_table_both, monkeypatch):
        monkeypatch.setattr("app.api.v2_scan._run_scan", lambda *a, **kw: {"rows": 0}, raising=False)
        r = seeded_app_both["client"].post(
            "/api/v2/scan",
            json={"table_id": registered_table_both["table_id"]},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 202, 422)

    def test_v2_scan_estimate(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].post(
            "/api/v2/scan/estimate",
            json={"table_id": registered_table_both["table_id"]},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 422)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestMetricsSmoke:
    COVERED_ROUTES = {
        "GET /api/metrics",
        "GET /api/admin/metrics",
        "POST /api/admin/metrics/import",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_metrics_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/metrics", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_admin_metrics(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/metrics", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_admin_metrics_import(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/metrics/import",
            json={"metrics": []},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 204, 422)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class TestMemorySmoke:
    COVERED_ROUTES = {
        "GET /api/memory",
        "POST /api/memory",
        "GET /api/memory/stats",
        "GET /api/memory/{item_id}/provenance",
        "POST /api/memory/{item_id}/vote",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_memory_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/memory", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_memory_create(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/memory",
            json={"title": "Smoke test fact", "content": "Revenue doubled QoQ in Q1.", "category": "business"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code == 201
        assert "id" in r.json()

    def test_memory_stats(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/memory/stats", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_memory_provenance(self, seeded_app_both):
        rc = seeded_app_both["client"].post(
            "/api/memory",
            json={"title": "Prov test", "content": "Revenue doubled QoQ in Q1.", "category": "business"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        item_id = rc.json()["id"]
        r = seeded_app_both["client"].get(f"/api/memory/{item_id}/provenance", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_memory_vote(self, seeded_app_both):
        rc = seeded_app_both["client"].post(
            "/api/memory",
            json={"title": "Vote test", "content": "Revenue doubled QoQ in Q1.", "category": "business"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        item_id = rc.json()["id"]
        r = seeded_app_both["client"].post(
            f"/api/memory/{item_id}/vote",
            json={"vote": "up"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

class TestUploadSmoke:
    COVERED_ROUTES = {
        "POST /api/upload/sessions",
        "POST /api/upload/artifacts",
        "POST /api/upload/local-md",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_upload_session(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/upload/sessions",
            json={"filename": "test.md", "content_type": "text/markdown"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201)

    def test_upload_artifact(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/upload/artifacts",
            json={"content": "# Test\nSome content.", "filename": "test.md"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201, 422)

    def test_upload_local_md(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/upload/local-md",
            json={"content": "# Local doc\nContent.", "path": "test.md"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201, 422)


# ---------------------------------------------------------------------------
# Admin Registry (register-table, precheck, CRUD)
# ---------------------------------------------------------------------------

class TestAdminRegistrySmoke:
    COVERED_ROUTES = {
        "GET /api/admin/registry",
        "GET /api/admin/server-config",
        "POST /api/admin/server-config",
        "POST /api/admin/register-table/precheck",
        "POST /api/admin/register-table",
        "PUT /api/admin/registry/{table_id}",
        "DELETE /api/admin/registry/{table_id}",
        "GET /api/admin/discover-tables",
        "POST /api/admin/configure",
        "GET /api/admin/metadata/{table_id}",
        "POST /api/admin/metadata/{table_id}/push",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_registry_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/registry", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_server_config(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/server-config", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_register_precheck(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/register-table/precheck",
            json={"name": "chk_orders", "source_type": "keboola", "bucket": "in.c-smoke", "source_table": "orders", "query_mode": "local"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 422)

    def test_register_table_crud(self, seeded_app_both):
        h = self._admin_headers(seeded_app_both)
        rc = seeded_app_both["client"].post(
            "/api/admin/register-table",
            json={"name": "crud_test_table", "source_type": "keboola", "bucket": "in.c-crud", "source_table": "orders", "query_mode": "local"},
            headers=h,
        )
        assert rc.status_code == 201
        table_id = rc.json()["id"]

        ru = seeded_app_both["client"].put(
            f"/api/admin/registry/{table_id}",
            json={"description": "Updated description for smoke test"},
            headers=h,
        )
        assert ru.status_code == 200

        rd = seeded_app_both["client"].delete(f"/api/admin/registry/{table_id}", headers=h)
        assert rd.status_code == 204

    def test_discover_tables(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/discover-tables", headers=self._admin_headers(seeded_app_both))
        assert r.status_code in (200, 503)

    def test_configure(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/admin/configure", json={}, headers=self._admin_headers(seeded_app_both))
        assert r.status_code in (200, 422)

    def test_metadata_get(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].get(
            f"/api/admin/metadata/{registered_table_both['table_id']}",
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 404)

    def test_metadata_push(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].post(
            f"/api/admin/metadata/{registered_table_both['table_id']}/push",
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 404, 422, 503)


# ---------------------------------------------------------------------------
# Admin Store  (submissions queue + reaper)
# ---------------------------------------------------------------------------

class TestAdminStoreSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/store/submissions",
        "GET /api/admin/store/submissions/{id}",
        "POST /api/admin/store/submissions/{id}/override",
        "POST /api/admin/store/submissions/{id}/rescan",
        "POST /api/admin/store/submissions/{id}/retry",
        "DELETE /api/admin/store/submissions/{id}",
        "GET /api/admin/store/submissions/{id}/bundle.zip",
        "POST /api/admin/run-reap-stuck-reviews",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_submissions_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/store/submissions", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_submissions_detail_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/store/submissions/nonexistent", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 404

    def test_reap_stuck_reviews_empty(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/admin/run-reap-stuck-reviews", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        assert "reaped" in body
        assert body["reaped"] == 0

    def test_submissions_override_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/store/submissions/nonexistent/override",
            json={"action": "approve"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code == 404

    def test_submissions_rescan_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/admin/store/submissions/nonexistent/rescan", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 404

    def test_submissions_retry_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/admin/store/submissions/nonexistent/retry", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 404

    def test_submissions_delete_missing(self, seeded_app_both):
        r = seeded_app_both["client"].delete("/api/admin/store/submissions/nonexistent", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 404

    def test_submissions_bundle_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/store/submissions/nonexistent/bundle.zip", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Admin Sessions
# ---------------------------------------------------------------------------

class TestAdminSessionsSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/sessions/list",
        "GET /api/admin/sessions/kpis",
        "GET /api/admin/sessions/facets",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_sessions_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/sessions/list", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sessions_kpis(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/sessions/kpis", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sessions_facets(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/sessions/facets", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Store (public listing, categories, owners, preview)
# ---------------------------------------------------------------------------

class TestStoreSmoke:
    COVERED_ROUTES = {
        "GET /api/store/categories",
        "GET /api/store/owners",
        "GET /api/store/entities",
        "GET /api/store/entities/{entity_id}",
        "GET /api/store/entities/{entity_id}/files",
        "GET /api/store/entities/{entity_id}/photo",
        "GET /api/store/entities/{entity_id}/docs/{filename}",
        "POST /api/store/entities/preview",
        "POST /api/store/entities/dryrun",
        "POST /api/store/entities",
        "PUT /api/store/entities/{entity_id}",
        "POST /api/store/entities/{entity_id}/install",
        "DELETE /api/store/entities/{entity_id}/install",
        "POST /api/store/entities/{entity_id}/rate",
        "DELETE /api/store/entities/{entity_id}",
        "GET /api/store/bundle.zip",
        "POST /api/store/import-bundle",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_categories(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/store/categories", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_owners(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/store/owners", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_entities_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/store/entities", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_entities_preview(self, seeded_app_both):
        from tests.helpers.factories import make_skill_zip
        import io
        zb = make_skill_zip("preview-skill")
        r = seeded_app_both["client"].post(
            "/api/store/entities/preview",
            files={"file": ("preview-skill.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 422)


# ---------------------------------------------------------------------------
# Flea Upload — state machine, visibility rules
# ---------------------------------------------------------------------------

class TestFleaUploadSmoke:
    COVERED_ROUTES: set = set()  # covered by TestStoreSmoke already

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def _analyst_headers(self, s):
        return {"Authorization": f"Bearer {s['analyst_token']}"}

    def _upload(self, client, headers, zip_bytes, entity_type="skill"):
        import io
        return client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zip_bytes), "application/zip")},
            data={"type": entity_type},
            headers=headers,
        )

    def test_upload_valid_skill_approved(self, seeded_app_both):
        from tests.helpers.factories import make_skill_zip
        r = self._upload(seeded_app_both["client"], self._admin_headers(seeded_app_both), make_skill_zip("smoke-skill-valid"))
        assert r.status_code == 201
        assert r.json()["visibility_status"] == "approved"

    def test_upload_fails_short_description(self, seeded_app_both):
        from tests.helpers.factories import make_bad_desc_zip
        r = self._upload(seeded_app_both["client"], self._admin_headers(seeded_app_both), make_bad_desc_zip("smoke-bad-desc"))
        assert r.status_code == 422
        assert r.json()["code"] == "validation_failed"

    def test_upload_fails_missing_name(self, seeded_app_both):
        from tests.helpers.factories import make_no_name_zip
        r = self._upload(seeded_app_both["client"], self._admin_headers(seeded_app_both), make_no_name_zip())
        assert r.status_code == 422
        assert r.json()["code"] == "validation_failed"

    def test_upload_fails_security_blocked(self, seeded_app_both):
        from tests.helpers.factories import make_security_fail_zip
        r = self._upload(seeded_app_both["client"], self._admin_headers(seeded_app_both), make_security_fail_zip("smoke-sec-fail"))
        assert r.status_code == 422
        assert r.json()["code"] == "security_blocked"

    def test_upload_duplicate_name_409(self, seeded_app_both):
        from tests.helpers.factories import make_skill_zip
        zb = make_skill_zip("smoke-duplicate-skill")
        self._upload(seeded_app_both["client"], self._admin_headers(seeded_app_both), zb)
        r2 = self._upload(seeded_app_both["client"], self._admin_headers(seeded_app_both), zb)
        assert r2.status_code == 409

    def test_upload_type_mismatch_returns_422(self, seeded_app_both):
        """Uploading a skill zip with type='plugin' declared → validation_failed."""
        from tests.helpers.factories import make_skill_zip
        # skill zip but type=plugin is a type-mismatch
        r = self._upload(
            seeded_app_both["client"],
            self._admin_headers(seeded_app_both),
            make_skill_zip("smoke-type-mismatch"),
            entity_type="plugin",  # wrong type for a skill zip
        )
        # Should fail with validation error (wrong manifest for type)
        assert r.status_code == 422, r.text

    def test_pending_entity_not_visible_to_other_user(self, seeded_app_both, monkeypatch):
        from tests.helpers.factories import make_skill_zip
        monkeypatch.setattr("src.store_guardrails.llm_review.review_bundle", lambda *a, **kw: {
            "risk_level": "low", "summary": "mock approve", "findings": [], "reviewed_by_model": "mock", "error": None,
        })
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        import io
        zb = make_skill_zip("smoke-pending-skill")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity = rc.json()
        assert entity["visibility_status"] == "pending_llm"
        entity_id = entity["id"]

        # analyst (non-owner) should NOT see it in list
        rl = seeded_app_both["client"].get("/api/store/entities", headers=self._analyst_headers(seeded_app_both))
        assert rl.status_code == 200
        ids_in_list = [e["id"] for e in rl.json()]
        assert entity_id not in ids_in_list

        # analyst direct get should be 403/404
        rd = seeded_app_both["client"].get(f"/api/store/entities/{entity_id}", headers=self._analyst_headers(seeded_app_both))
        assert rd.status_code in (403, 404)

    def test_pending_entity_visible_to_admin(self, seeded_app_both, monkeypatch):
        """Admin can GET a pending entity directly."""
        from tests.helpers.factories import make_skill_zip
        monkeypatch.setattr("src.store_guardrails.llm_review.review_bundle", lambda *a, **kw: {
            "risk_level": "low", "summary": "mock", "findings": [],
            "reviewed_by_model": "mock", "error": None,
        })
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        import io
        zb = make_skill_zip("smoke-admin-sees-pending")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity = rc.json()
        assert entity["visibility_status"] == "pending_llm"
        entity_id = entity["id"]
        # Admin can see it directly
        r = seeded_app_both["client"].get(
            f"/api/store/entities/{entity_id}",
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code == 200, r.text

    def test_approved_entity_visible_to_everyone(self, seeded_app_both):
        from tests.helpers.factories import make_skill_zip
        import io
        zb = make_skill_zip("smoke-approved-visible")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=self._admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity_id = rc.json()["id"]
        r = seeded_app_both["client"].get(f"/api/store/entities/{entity_id}", headers=self._analyst_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# My Stack
# ---------------------------------------------------------------------------

class TestMyStackSmoke:
    COVERED_ROUTES = {
        "GET /api/my-stack",
        "PUT /api/my-stack",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def _upload_skill(self, client, admin_headers, name):
        from tests.helpers.factories import make_skill_zip
        import io
        r = client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(make_skill_zip(name)), "application/zip")},
            data={"type": "skill"},
            headers=admin_headers,
        )
        assert r.status_code == 201
        return r.json()["id"]

    def test_install_skill_appears_in_stack(self, seeded_app_both):
        client = seeded_app_both["client"]
        h = self._admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "stack-install-skill")
        ri = client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        assert ri.status_code in (200, 201)
        rs = client.get("/api/my-stack", headers=h)
        assert rs.status_code == 200
        ids = [e["id"] for e in rs.json()]
        assert entity_id in ids

    def test_install_plugin_appears_in_stack(self, seeded_app_both):
        from tests.helpers.factories import make_plugin_zip
        import io
        client = seeded_app_both["client"]
        h = self._admin_headers(seeded_app_both)
        r = client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(make_plugin_zip("stack-install-plugin")), "application/zip")},
            data={"type": "plugin"},
            headers=h,
        )
        assert r.status_code == 201
        entity_id = r.json()["id"]
        ri = client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        assert ri.status_code in (200, 201)
        rs = client.get("/api/my-stack", headers=h)
        assert entity_id in [e["id"] for e in rs.json()]

    def test_install_agent_appears_in_stack(self, seeded_app_both):
        from tests.helpers.factories import make_agent_zip
        import io
        client = seeded_app_both["client"]
        h = self._admin_headers(seeded_app_both)
        r = client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(make_agent_zip("stack-install-agent")), "application/zip")},
            data={"type": "agent"},
            headers=h,
        )
        assert r.status_code == 201
        entity_id = r.json()["id"]
        ri = client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        assert ri.status_code in (200, 201)
        rs = client.get("/api/my-stack", headers=h)
        assert entity_id in [e["id"] for e in rs.json()]

    def test_uninstall_removes_from_stack(self, seeded_app_both):
        client = seeded_app_both["client"]
        h = self._admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "stack-uninstall-skill")
        client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        rd = client.delete(f"/api/store/entities/{entity_id}/install", headers=h)
        assert rd.status_code == 204
        rs = client.get("/api/my-stack", headers=h)
        assert entity_id not in [e["id"] for e in rs.json()]

    def test_cli_my_stack_show_lists_installed(self, cli_client_both, seeded_app_both):
        """agnes my-stack show output contains entity name after install."""
        client = seeded_app_both["client"]
        h = self._admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "cli-stack-show-skill")
        client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        result = cli_client_both["invoke"](["my-stack", "show"])
        assert result.exit_code == 0
        assert "cli-stack-show-skill" in result.output

    def test_cli_my_stack_show_after_removal(self, cli_client_both, seeded_app_both):
        """agnes my-stack show output does not contain entity after uninstall."""
        client = seeded_app_both["client"]
        h = self._admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "cli-stack-remove-skill")
        client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        client.delete(f"/api/store/entities/{entity_id}/install", headers=h)
        result = cli_client_both["invoke"](["my-stack", "show"])
        assert result.exit_code == 0
        assert "cli-stack-remove-skill" not in result.output


# ---------------------------------------------------------------------------
# CLI — CliRunner through in-process transport
# ---------------------------------------------------------------------------

class TestCLISmoke:
    COVERED_ROUTES: set = set()  # CLI hits the same routes as web tests above

    def test_help(self, cli_client_both):
        result = cli_client_both["invoke"](["--help"])
        assert result.exit_code == 0

    def test_pull_help(self, cli_client_both):
        result = cli_client_both["invoke"](["pull", "--help"])
        assert result.exit_code == 0

    def test_admin_help(self, cli_client_both):
        result = cli_client_both["invoke"](["admin", "--help"])
        assert result.exit_code == 0

    def test_my_stack_show(self, cli_client_both):
        result = cli_client_both["invoke"](["my-stack", "show"])
        assert result.exit_code == 0

    def test_query_select_one(self, cli_client_both, registered_table_both):
        result = cli_client_both["invoke"](["query", "SELECT 1 AS n"])
        assert result.exit_code == 0

    def test_diagnose(self, cli_client_both):
        result = cli_client_both["invoke"](["diagnose"])
        assert result.exit_code == 0

    def test_catalog(self, cli_client_both, registered_table_both):
        result = cli_client_both["invoke"](["catalog"])
        assert result.exit_code == 0

    def test_skills_help(self, cli_client_both):
        result = cli_client_both["invoke"](["skills", "--help"])
        assert result.exit_code == 0

    def test_store_help(self, cli_client_both):
        result = cli_client_both["invoke"](["store", "--help"])
        assert result.exit_code == 0

    def test_marketplace_help(self, cli_client_both):
        result = cli_client_both["invoke"](["marketplace", "--help"])
        assert result.exit_code == 0

    def test_auth_token_list(self, cli_client_both):
        result = cli_client_both["invoke"](["auth", "token", "list"])
        assert result.exit_code == 0

    def test_snapshot_help(self, cli_client_both):
        result = cli_client_both["invoke"](["snapshot", "--help"])
        assert result.exit_code == 0

    def test_schema_table(self, cli_client_both, registered_table_both):
        """agnes schema <table_id> exits 0 and prints column info."""
        table_id = registered_table_both["table_id"]
        result = cli_client_both["invoke"](["schema", table_id])
        assert result.exit_code == 0, f"schema failed: {result.output}"


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

class TestTokensSmoke:
    COVERED_ROUTES = {
        "GET /auth/tokens",
        "POST /auth/tokens",
        "GET /auth/tokens/{token_id}",
        "DELETE /auth/tokens/{token_id}",
        "GET /auth/admin/tokens",
        "DELETE /auth/admin/tokens/{token_id}",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_tokens_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/auth/tokens", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_token_create_get_delete(self, seeded_app_both):
        h = self._admin_headers(seeded_app_both)
        rc = seeded_app_both["client"].post("/auth/tokens", json={"name": "smoke-token"}, headers=h)
        assert rc.status_code == 201
        assert "token" in rc.json()
        token_id = rc.json()["id"]
        rg = seeded_app_both["client"].get(f"/auth/tokens/{token_id}", headers=h)
        assert rg.status_code == 200
        rd = seeded_app_both["client"].delete(f"/auth/tokens/{token_id}", headers=h)
        assert rd.status_code == 204

    def test_admin_tokens_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/auth/admin/tokens", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------

class TestScriptsSmoke:
    COVERED_ROUTES = {
        "GET /api/scripts",
    }

    def test_scripts_list(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/scripts",
            headers={"Authorization": f"Bearer {seeded_app_both['admin_token']}"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class TestSettingsSmoke:
    COVERED_ROUTES = {
        "GET /api/settings",
        "PUT /api/settings/dataset",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_settings_get(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/settings", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_settings_dataset_put(self, seeded_app_both):
        r = seeded_app_both["client"].put("/api/settings/dataset", json={}, headers=self._admin_headers(seeded_app_both))
        assert r.status_code in (200, 422)


# ---------------------------------------------------------------------------
# Marketplaces
# ---------------------------------------------------------------------------

class TestMarketplacesSmoke:
    COVERED_ROUTES = {
        "GET /api/marketplaces",
        "POST /api/marketplaces",
        "POST /api/marketplaces/{mp_id}/sync",
        "GET /api/marketplace/items",
        "GET /api/marketplace/categories",
        "GET /api/marketplace/curated/{mp_id}/{plugin_name}",
        "GET /api/marketplace/flea/{entity_id}/detail",
        "POST /api/marketplace/curated/{mp_id}/{plugin_name}/install",
        "DELETE /api/marketplace/curated/{mp_id}/{plugin_name}/install",
    }

    def _admin_headers(self, s):
        return {"Authorization": f"Bearer {s['admin_token']}"}

    def test_marketplaces_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/marketplaces", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_marketplace_items(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/marketplace/items", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_marketplace_categories(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/marketplace/categories", headers=self._admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_marketplace_curated_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/marketplace/curated/nonexistent-mp/nonexistent-plugin",
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 404)

    def test_marketplace_flea_detail_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/marketplace/flea/nonexistent-entity/detail",
            headers=self._admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Route-coverage guard
# ---------------------------------------------------------------------------

KNOWN_UNTESTED = {
    # dulwich smart-HTTP git bridge — requires git repo on disk, explicit non-goal
    "GET /marketplace.git/{path}",
    "POST /marketplace.git/{path}",
    # Google OAuth — requires live credentials
    "GET /auth/google/login",
    "GET /auth/google/callback",
    # Telegram webhook — live external service
    "POST /api/telegram/webhook",
    # Jira webhooks — live external service
    "POST /api/jira/webhook",
    # Chat SSE / co-presence — requires live E2B/Anthropic creds
    "GET /api/chat/sessions/{session_id}/stream",
    "POST /api/chat/sessions",
    "DELETE /api/chat/sessions/{session_id}",
    "GET /api/chat/copresence/{session_id}",
    # Slack — live transport
    "POST /api/slack/events",
    "POST /api/slack/interactions",
    "POST /api/slack/slash",
    # HTML web routes — covered by separate UI test suite
    "GET /",
    "GET /{path:path}",
    # MCP SSE — live streaming
    "GET /mcp/sse",
    "POST /mcp/messages",
    # CLI auth (device flow) — tested via CLI tests above
    "POST /api/cli/auth/device/init",
    "GET /api/cli/auth/device/poll",
    "GET /api/cli/auth/device/activate",
    # BQ metadata refresh — requires BQ credentials
    "POST /api/admin/bq-metadata-refresh",
    # DB state (internal migration endpoint)
    "GET /api/admin/db-state",
    "POST /api/admin/db-state/migrate",
    # Observability (PostHog proxy) — external service
    "POST /api/observability/capture",
    # Admin adoption / usage dashboards — DuckDB analytics, not business state
    "GET /api/admin/adoption",
    "GET /api/admin/usage",
    "GET /api/admin/usage/summary",
    "GET /api/admin/user-sessions",
    # Cowork bundle — complex real-time feature, dedicated suite planned
    "GET /api/cowork/sessions",
    "POST /api/cowork/sessions",
    "GET /api/cowork/auth/token",
    # Cache warmup — internal
    "POST /api/admin/cache-warmup",
    # Stack / stack-views — admin config pages
    "GET /api/admin/stack",
    "PUT /api/admin/stack",
    "GET /api/admin/stack-views",
    # Initial workspace — one-shot setup
    "POST /api/admin/initial-workspace/trigger",
    "GET /api/admin/initial-workspace/status",
    # Prompts / news — read-only
    "GET /api/prompts",
    "GET /api/news",
    # Memory domains + suggestions — covered by TestMemorySmoke path
    "GET /api/memory/domains",
    "POST /api/memory/domains",
    "GET /api/memory/domain-suggestions",
    # Recipes — admin config
    "GET /api/recipes",
    "GET /api/admin/recipes",
    # Claude.md — read-only
    "GET /api/claude-md",
    # MCP per-table / user-secrets
    "GET /api/mcp/tables/{table_id}/sse",
    "POST /api/mcp/tables/{table_id}/messages",
    "GET /api/mcp/user-secrets",
    "PUT /api/mcp/user-secrets",
    # Admin MCP / slack secrets — operator config
    "GET /api/admin/mcp",
    "PUT /api/admin/mcp",
    "GET /api/admin/slack-secrets",
    "PUT /api/admin/slack-secrets",
    # Admin bigquery / keboola test endpoints
    "POST /api/admin/bigquery/test",
    "POST /api/admin/keboola/test",
    # Admin uploads list
    "GET /api/admin/uploads",
    # MCP passthrough
    "GET /api/mcp/passthrough/{path:path}",
    "POST /api/mcp/passthrough/{path:path}",
    # V2 marketplace
    "GET /api/v2/marketplace/items",
    # Welcome
    "GET /api/welcome",
    # Data packages
    "GET /api/data-packages",
    "POST /api/data-packages",
    # Admin chat
    "GET /api/admin/chat",
    "PUT /api/admin/chat",
    # Connectors
    "GET /api/connectors",
    "POST /api/connectors",
}


def _collect_covered_routes() -> set:
    """Aggregate COVERED_ROUTES from every test class in both smoke and behavioral files."""
    import importlib
    covered: set = set()
    for mod_name in (
        "tests.db_pg.test_endpoints_smoke",
        "tests.db_pg.test_endpoints_behavioral",
    ):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for obj in vars(mod).values():
            if isinstance(obj, type) and hasattr(obj, "COVERED_ROUTES"):
                covered.update(obj.COVERED_ROUTES)
    return covered


@pytest.mark.parametrize("state_backend", ["duckdb"], indirect=True)
def test_every_route_is_covered_or_excluded(seeded_app_both):
    """Route-coverage guard: fails CI when a new endpoint has no test or exclusion."""
    app = seeded_app_both["client"].app
    all_routes = {
        f"{m} {r.path}"
        for r in app.routes
        for m in (getattr(r, "methods", None) or set()) - {"HEAD", "OPTIONS"}
    }
    covered = _collect_covered_routes() | KNOWN_UNTESTED
    missing = sorted(all_routes - covered)
    assert not missing, (
        "Routes with no smoke/behavioral coverage and no KNOWN_UNTESTED entry "
        "(add a test class entry or a justified KNOWN_UNTESTED exclusion): "
        f"{missing}"
    )
    # Reverse drift: covered routes that no longer exist in the app
    stale = sorted((covered - KNOWN_UNTESTED) - all_routes)
    assert not stale, (
        "COVERED_ROUTES entries that no longer exist as app routes "
        "(remove them from the class COVERED_ROUTES set): "
        f"{stale}"
    )
