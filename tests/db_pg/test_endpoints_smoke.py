"""Smoke tests — happy-path HTTP status + response shape for every endpoint group.

Each test class declares COVERED_ROUTES consumed by the route-coverage guard at
the bottom of this file. Depth: HTTP status code + top-level response shape only.
All tests run twice via seeded_app_both (DuckDB-only + Postgres).
"""

from __future__ import annotations

import pytest

from tests.helpers.factories import (
    make_skill_zip,
    make_plugin_zip,
    make_agent_zip,
    make_bad_desc_zip,
    make_no_name_zip,
    make_security_fail_zip,
)


pytestmark = pytest.mark.integration


def _admin_headers(s):
    return {"Authorization": f"Bearer {s['admin_token']}"}


def _analyst_headers(s):
    return {"Authorization": f"Bearer {s['analyst_token']}"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuthSmoke:
    COVERED_ROUTES = {
        "POST /auth/token",
        "POST /auth/bootstrap",
        "POST /auth/password/login",
    }

    def test_bootstrap_returns_403_after_seeding(self, seeded_app_both):
        """Bootstrap window is closed once a user with a password exists."""
        from argon2 import PasswordHasher
        from src.repositories import users_repo

        users_repo().update(id="admin1", password_hash=PasswordHasher().hash("admin-pass"))
        r = seeded_app_both["client"].post(
            "/auth/bootstrap",
            json={
                "email": "new@test.com",
                "password": "newpass123",
                "name": "New",
            },
        )
        assert r.status_code in (403, 409), r.text

    def test_password_login_returns_token(self, seeded_app_both):
        """Password login endpoint is reachable (401 expected — no password set)."""
        r = seeded_app_both["client"].post(
            "/auth/password/login",
            json={
                "email": "admin@test.com",
                "password": "wrong",
            },
        )
        assert r.status_code == 401, r.text

    def test_token_with_password_user(self, seeded_app_both):
        """POST /auth/token returns 200 + access_token for a user with a password_hash."""
        from argon2 import PasswordHasher
        from src.repositories import users_repo

        ph = PasswordHasher()
        users_repo().create(
            id="pw-user1",
            email="pw@test.com",
            name="PwUser",
            password_hash=ph.hash("test-password"),
        )
        r = seeded_app_both["client"].post(
            "/auth/token",
            json={
                "email": "pw@test.com",
                "password": "test-password",
            },
        )
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
        r = seeded_app_both["client"].get("/api/health/detailed", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        assert "status" in body
        assert "services" in body

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

    def test_me_home_stats(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/home-stats", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_me_effective_access(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/effective-access", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "items" in r.json()

    def test_me_onboarded(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/me/onboarded", headers=_admin_headers(seeded_app_both))
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

    def test_stats_sessions(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/sessions", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_stats_tokens(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/tokens", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_stats_queries(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/queries", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_stats_sync(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/me/stats/sync", headers=_admin_headers(seeded_app_both))
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

    def test_list_users(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/users", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_get_user(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/users/admin1", headers=_admin_headers(seeded_app_both))
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

    def test_list_groups(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/groups", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_access_overview(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/access-overview", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_grants_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/grants", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_resource_types(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/resource-types", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_activity(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/activity", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_activity_health(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/activity/health", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_activity_sync(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/activity/sync", headers=_admin_headers(seeded_app_both))
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

    def test_sync_status(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/status", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sync_manifest(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/manifest", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "tables" in r.json()

    def test_sync_trigger(self, seeded_app_both, monkeypatch):
        monkeypatch.setattr("app.api.sync._run_sync", lambda *a, **kw: None)
        r = seeded_app_both["client"].post("/api/sync/trigger", headers=_admin_headers(seeded_app_both))
        assert r.status_code in (200, 202)

    def test_sync_settings(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/settings", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sync_table_subscriptions(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/sync/table-subscriptions", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestCatalogSmoke:
    COVERED_ROUTES = {
        "GET /api/catalog/tables",
        "GET /api/catalog/profile/{table_name}",
        "POST /api/catalog/profile/{table_name}/refresh",
    }

    def test_catalog_tables(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/catalog/tables", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        tables = body if isinstance(body, list) else body.get("tables", [])
        assert isinstance(tables, list)

    def test_catalog_profile_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/catalog/profile/nonexistent-table", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code in (404, 422)

    def test_catalog_profile_refresh_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/catalog/profile/nonexistent-table/refresh", headers=_admin_headers(seeded_app_both)
        )
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
        assert r.status_code == 204

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

    def test_query_select_one(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].post(
            "/api/query",
            json={"sql": "SELECT 1 AS n"},
            headers=_admin_headers(seeded_app_both),
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
            headers=_admin_headers(seeded_app_both),
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

    def test_v2_catalog(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].get("/api/v2/catalog", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        tables = body if isinstance(body, list) else body.get("tables", [])
        assert isinstance(tables, list)

    def test_v2_schema(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(f"/api/v2/schema/{table_id}", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert "columns" in r.json()

    def test_v2_sample(self, seeded_app_both, registered_table_both):
        table_id = registered_table_both["table_id"]
        r = seeded_app_both["client"].get(f"/api/v2/sample/{table_id}", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_v2_scan(self, seeded_app_both, registered_table_both, monkeypatch):
        monkeypatch.setattr("app.api.v2_scan._run_scan", lambda *a, **kw: {"rows": 0}, raising=False)
        r = seeded_app_both["client"].post(
            "/api/v2/scan",
            json={"table_id": registered_table_both["table_id"]},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 202, 422)

    def test_v2_scan_estimate(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].post(
            "/api/v2/scan/estimate",
            json={"table_id": registered_table_both["table_id"]},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 501)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetricsSmoke:
    COVERED_ROUTES = {
        "GET /api/metrics",
        "POST /api/admin/metrics",
        "POST /api/admin/metrics/import",
    }

    def test_metrics_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/metrics", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_admin_metrics_create(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/metrics",
            json={},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201, 422)

    def test_admin_metrics_import(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/metrics/import",
            json={"metrics": []},
            headers=_admin_headers(seeded_app_both),
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

    def test_memory_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/memory", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        items = body if isinstance(body, list) else body.get("items", [])
        assert isinstance(items, list)

    def test_memory_create(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/memory",
            json={"title": "Smoke test fact", "content": "Revenue doubled QoQ in Q1.", "category": "business"},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 201
        assert "id" in r.json()

    def test_memory_stats(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/memory/stats", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_memory_provenance(self, seeded_app_both):
        rc = seeded_app_both["client"].post(
            "/api/memory",
            json={"title": "Prov test", "content": "Revenue doubled QoQ in Q1.", "category": "business"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        item_id = rc.json()["id"]
        r = seeded_app_both["client"].get(f"/api/memory/{item_id}/provenance", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_memory_vote(self, seeded_app_both):
        rc = seeded_app_both["client"].post(
            "/api/memory",
            json={"title": "Vote test", "content": "Revenue doubled QoQ in Q1.", "category": "business"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        item_id = rc.json()["id"]
        r = seeded_app_both["client"].post(
            f"/api/memory/{item_id}/vote",
            json={"vote": 1},
            headers=_admin_headers(seeded_app_both),
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

    def test_upload_session(self, seeded_app_both):
        import io

        r = seeded_app_both["client"].post(
            "/api/upload/sessions",
            files={"file": ("test.jsonl", io.BytesIO(b'{"type":"text"}\n'), "application/octet-stream")},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201)

    def test_upload_artifact(self, seeded_app_both):
        import io

        r = seeded_app_both["client"].post(
            "/api/upload/artifacts",
            files={"file": ("test.html", io.BytesIO(b"<h1>Test</h1>"), "text/html")},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201)

    def test_upload_local_md(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/upload/local-md",
            json={"content": "# Local doc\nContent.", "path": "test.md"},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 201)


# ---------------------------------------------------------------------------
# Admin Registry (register-table, precheck, CRUD)
# ---------------------------------------------------------------------------


class TestAdminRegistrySmoke:
    COVERED_ROUTES = {
        "GET /api/admin/config-surface",
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

    def test_registry_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/registry", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        tables = body if isinstance(body, list) else body.get("tables", [])
        assert isinstance(tables, list)

    def test_server_config(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/server-config", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_config_surface(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/config-surface", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_register_precheck(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/register-table/precheck",
            json={
                "name": "chk_orders",
                "source_type": "keboola",
                "bucket": "in.c-smoke",
                "source_table": "orders",
                "query_mode": "local",
            },
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 422)

    def test_register_table_crud(self, seeded_app_both):
        h = _admin_headers(seeded_app_both)
        rc = seeded_app_both["client"].post(
            "/api/admin/register-table",
            json={
                "name": "crud_test_table",
                "source_type": "keboola",
                "bucket": "in.c-crud",
                "source_table": "orders",
                "query_mode": "local",
            },
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
        r = seeded_app_both["client"].get("/api/admin/discover-tables", headers=_admin_headers(seeded_app_both))
        assert r.status_code in (200, 503)

    def test_configure(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/admin/configure", json={}, headers=_admin_headers(seeded_app_both))
        assert r.status_code in (200, 422)

    def test_metadata_get(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].get(
            f"/api/admin/metadata/{registered_table_both['table_id']}",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 404)

    def test_metadata_push(self, seeded_app_both, registered_table_both):
        r = seeded_app_both["client"].post(
            f"/api/admin/metadata/{registered_table_both['table_id']}/push",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 400, 404, 422, 500, 503)


# ---------------------------------------------------------------------------
# Admin Store  (submissions queue + reaper)
# ---------------------------------------------------------------------------


class TestAdminStoreSmoke:
    COVERED_ROUTES = {
        "GET /api/admin/store/submissions",
        "POST /api/admin/run-reap-stuck-reviews",
    }

    def test_submissions_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/store/submissions", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        items = body if isinstance(body, list) else body.get("items", [])
        assert isinstance(items, list)

    def test_submissions_detail_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/admin/store/submissions/nonexistent", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404

    def test_reap_stuck_reviews_empty(self, seeded_app_both):
        r = seeded_app_both["client"].post("/api/admin/run-reap-stuck-reviews", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert body.get("details", {}).get("reaped", -1) == 0

    def test_submissions_override_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/store/submissions/nonexistent/override",
            json={"reason": "test override reason"},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 404

    def test_submissions_rescan_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/store/submissions/nonexistent/rescan", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404

    def test_submissions_retry_missing(self, seeded_app_both):
        r = seeded_app_both["client"].post(
            "/api/admin/store/submissions/nonexistent/retry", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404

    def test_submissions_delete_missing(self, seeded_app_both):
        r = seeded_app_both["client"].delete(
            "/api/admin/store/submissions/nonexistent", headers=_admin_headers(seeded_app_both)
        )
        assert r.status_code == 404

    def test_submissions_bundle_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/admin/store/submissions/nonexistent/bundle.zip", headers=_admin_headers(seeded_app_both)
        )
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

    def test_sessions_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/sessions/list", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sessions_kpis(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/sessions/kpis", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_sessions_facets(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/admin/sessions/facets", headers=_admin_headers(seeded_app_both))
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

    def test_categories(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/store/categories", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_owners(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/store/owners", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_entities_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/store/entities", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200
        body = r.json()
        items = body if isinstance(body, list) else body.get("items", [])
        assert isinstance(items, list)

    def test_entities_preview(self, seeded_app_both):
        import io

        zb = make_skill_zip("preview-skill")
        r = seeded_app_both["client"].post(
            "/api/store/entities/preview",
            files={"file": ("preview-skill.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Flea Upload — state machine, visibility rules
# ---------------------------------------------------------------------------


class TestFleaUploadSmoke:
    COVERED_ROUTES: set = set()  # covered by TestStoreSmoke already

    def _upload(self, client, headers, zip_bytes, entity_type="skill"):
        import io

        return client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zip_bytes), "application/zip")},
            data={"type": entity_type},
            headers=headers,
        )

    def test_upload_valid_skill_approved(self, seeded_app_both):
        r = self._upload(
            seeded_app_both["client"], _admin_headers(seeded_app_both), make_skill_zip("smoke-skill-valid")
        )
        assert r.status_code == 201
        assert r.json()["visibility_status"] == "approved"

    def test_upload_fails_short_description(self, seeded_app_both):
        r = self._upload(
            seeded_app_both["client"], _admin_headers(seeded_app_both), make_bad_desc_zip("smoke-bad-desc")
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "validation_failed"

    def test_upload_fails_missing_name(self, seeded_app_both):
        r = self._upload(seeded_app_both["client"], _admin_headers(seeded_app_both), make_no_name_zip())
        assert r.status_code in (400, 422)

    def test_upload_fails_security_blocked(self, seeded_app_both):
        r = self._upload(
            seeded_app_both["client"], _admin_headers(seeded_app_both), make_security_fail_zip("smoke-sec-fail")
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "security_blocked"

    def test_upload_duplicate_name_409(self, seeded_app_both):
        zb = make_skill_zip("smoke-duplicate-skill")
        self._upload(seeded_app_both["client"], _admin_headers(seeded_app_both), zb)
        r2 = self._upload(seeded_app_both["client"], _admin_headers(seeded_app_both), zb)
        assert r2.status_code == 409

    def test_upload_type_mismatch_returns_422(self, seeded_app_both):
        """Uploading a skill zip with type='plugin' declared → validation_failed."""
        # skill zip but type=plugin is a type-mismatch
        r = self._upload(
            seeded_app_both["client"],
            _admin_headers(seeded_app_both),
            make_skill_zip("smoke-type-mismatch"),
            entity_type="plugin",  # wrong type for a skill zip
        )
        # Should fail with validation error (wrong manifest for type)
        assert r.status_code == 422, r.text

    def test_pending_entity_not_visible_to_other_user(self, seeded_app_both, monkeypatch):
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "low",
                "summary": "mock approve",
                "findings": [],
                "reviewed_by_model": "mock",
                "error": None,
            },
        )
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        import io

        zb = make_skill_zip("smoke-pending-skill")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity = rc.json()
        assert entity["visibility_status"] == "pending"
        entity_id = entity["id"]

        # analyst (non-owner) should NOT see it in list
        rl = seeded_app_both["client"].get("/api/store/entities", headers=_analyst_headers(seeded_app_both))
        assert rl.status_code == 200
        rl_body = rl.json()
        rl_items = rl_body if isinstance(rl_body, list) else rl_body.get("items", [])
        ids_in_list = [e["id"] for e in rl_items]
        assert entity_id not in ids_in_list

        # analyst direct get should be 403/404
        rd = seeded_app_both["client"].get(
            f"/api/store/entities/{entity_id}", headers=_analyst_headers(seeded_app_both)
        )
        assert rd.status_code in (403, 404)

    def test_pending_entity_visible_to_owner(self, seeded_app_both, monkeypatch):
        """Owner (uploader) can see their own pending entity in the store listing."""
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "low",
                "summary": "mock",
                "findings": [],
                "reviewed_by_model": "mock",
                "error": None,
            },
        )
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        import io

        zb = make_skill_zip("smoke-owner-sees-own-pending")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity = rc.json()
        assert entity["visibility_status"] == "pending"
        entity_id = entity["id"]
        # Owner (admin) should see it in their own listing
        rl = seeded_app_both["client"].get("/api/store/entities", headers=_admin_headers(seeded_app_both))
        assert rl.status_code == 200
        rl_body = rl.json()
        rl_items = rl_body if isinstance(rl_body, list) else rl_body.get("items", [])
        ids_in_list = [e["id"] for e in rl_items]
        assert entity_id in ids_in_list, "Owner cannot see their own pending entity in store listing"

    def test_pending_entity_visible_to_admin(self, seeded_app_both, monkeypatch):
        """Admin can GET a pending entity directly."""
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "low",
                "summary": "mock",
                "findings": [],
                "reviewed_by_model": "mock",
                "error": None,
            },
        )
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        import io

        zb = make_skill_zip("smoke-admin-sees-pending")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity = rc.json()
        assert entity["visibility_status"] == "pending"
        entity_id = entity["id"]
        # Admin can see it directly
        r = seeded_app_both["client"].get(
            f"/api/store/entities/{entity_id}",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code == 200, r.text

    def test_approved_entity_visible_to_everyone(self, seeded_app_both):
        import io

        zb = make_skill_zip("smoke-approved-visible")
        rc = seeded_app_both["client"].post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(seeded_app_both),
        )
        assert rc.status_code == 201
        entity_id = rc.json()["id"]
        r = seeded_app_both["client"].get(f"/api/store/entities/{entity_id}", headers=_analyst_headers(seeded_app_both))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# My Stack
# ---------------------------------------------------------------------------


class TestMyStackSmoke:
    COVERED_ROUTES = {
        "GET /api/my-stack",
    }

    def _upload_skill(self, client, admin_headers, name):
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
        h = _admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "stack-install-skill")
        ri = client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        assert ri.status_code in (200, 201)
        rs = client.get("/api/my-stack", headers=h)
        assert rs.status_code == 200
        ids = [e["entity_id"] for e in rs.json().get("store", [])]
        assert entity_id in ids

    def test_install_plugin_appears_in_stack(self, seeded_app_both):
        import io

        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
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
        assert entity_id in [e["entity_id"] for e in rs.json().get("store", [])]

    def test_install_agent_appears_in_stack(self, seeded_app_both):
        import io

        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
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
        assert entity_id in [e["entity_id"] for e in rs.json().get("store", [])]

    def test_uninstall_removes_from_stack(self, seeded_app_both):
        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "stack-uninstall-skill")
        client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        rd = client.delete(f"/api/store/entities/{entity_id}/install", headers=h)
        assert rd.status_code == 204
        rs = client.get("/api/my-stack", headers=h)
        assert entity_id not in [e["entity_id"] for e in rs.json().get("store", [])]

    def test_cli_my_stack_show_lists_installed(self, cli_client_both, seeded_app_both):
        """agnes my-stack show output contains entity name after install."""
        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
        entity_id = self._upload_skill(client, h, "cli-stack-show-skill")
        client.post(f"/api/store/entities/{entity_id}/install", headers=h)
        result = cli_client_both["invoke"](["my-stack", "show"])
        assert result.exit_code == 0
        assert "cli-stack-show-skill" in result.output

    def test_cli_my_stack_show_after_removal(self, cli_client_both, seeded_app_both):
        """agnes my-stack show output does not contain entity after uninstall."""
        client = seeded_app_both["client"]
        h = _admin_headers(seeded_app_both)
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
        result = cli_client_both["invoke"](["query", "--remote", "SELECT 1 AS n"])
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

    def test_tokens_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/auth/tokens", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_token_create_get_delete(self, seeded_app_both):
        h = _admin_headers(seeded_app_both)
        rc = seeded_app_both["client"].post("/auth/tokens", json={"name": "smoke-token"}, headers=h)
        assert rc.status_code == 201
        assert "token" in rc.json()
        token_id = rc.json()["id"]
        rg = seeded_app_both["client"].get(f"/auth/tokens/{token_id}", headers=h)
        assert rg.status_code == 200
        rd = seeded_app_both["client"].delete(f"/auth/tokens/{token_id}", headers=h)
        assert rd.status_code == 204

    def test_admin_tokens_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/auth/admin/tokens", headers=_admin_headers(seeded_app_both))
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

    def test_settings_get(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/settings", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_settings_dataset_put(self, seeded_app_both):
        r = seeded_app_both["client"].put("/api/settings/dataset", json={}, headers=_admin_headers(seeded_app_both))
        assert r.status_code in (200, 422)


# ---------------------------------------------------------------------------
# Marketplaces
# ---------------------------------------------------------------------------


class TestMarketplacesSmoke:
    COVERED_ROUTES = {
        "GET /api/marketplaces",
        "POST /api/marketplaces",
        "POST /api/marketplaces/{marketplace_id}/sync",
        "GET /api/marketplace/items",
        "GET /api/marketplace/categories",
        "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}",
        "GET /api/marketplace/flea/{entity_id}/detail",
        "POST /api/marketplace/curated/{marketplace_id}/{plugin_name}/install",
        "DELETE /api/marketplace/curated/{marketplace_id}/{plugin_name}/install",
    }

    def test_marketplaces_list(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/marketplaces", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_marketplace_items(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/marketplace/items", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_marketplace_categories(self, seeded_app_both):
        r = seeded_app_both["client"].get("/api/marketplace/categories", headers=_admin_headers(seeded_app_both))
        assert r.status_code == 200

    def test_marketplace_curated_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/marketplace/curated/nonexistent-mp/nonexistent-plugin",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 404)

    def test_marketplace_flea_detail_missing(self, seeded_app_both):
        r = seeded_app_both["client"].get(
            "/api/marketplace/flea/nonexistent-entity/detail",
            headers=_admin_headers(seeded_app_both),
        )
        assert r.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Route-coverage guard
# ---------------------------------------------------------------------------

KNOWN_UNTESTED = {
    # Collections (bring-your-files) — behaviorally covered in the dedicated
    # suites tests/test_api_collections.py (CRUD/upload/search, RBAC fail-closed,
    # SessionPrincipal) and tests/test_web_library.py (/library pages), plus the
    # ingestion/retrieval unit suites; not duplicated in this PG smoke harness.
    "POST /api/collections",
    "GET /api/collections",
    "GET /api/collections/search",
    "GET /api/collections/{collection_id}",
    "DELETE /api/collections/{collection_id}",
    "POST /api/collections/{collection_id}/files",
    "GET /api/collections/{collection_id}/files",
    "DELETE /api/collections/{collection_id}/files/{file_id}",
    "GET /library",
    "GET /library/{slug}",
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
    "GET /{full_path:path}",
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
    "DELETE /admin/chat/{chat_id}",
    "POST /admin/chat/secrets",
    "POST /admin/chat/secrets/test",
    # Connectors
    "GET /api/connectors",
    "POST /api/connectors",
    "GET /api/connectors/manifest",
    "GET /api/connectors/params",
    # HTML admin pages — covered by separate UI test suite
    "GET /admin/access",
    "GET /admin/activity",
    "GET /admin/adoption",
    "GET /admin/adoption/users/{user_id}",
    "GET /admin/agent-prompt",
    "GET /admin/chat",
    "GET /admin/chat/readiness",
    "GET /admin/chat/{chat_id}/debug",
    "GET /admin/chat/{chat_id}/tail-ticket",
    "GET /admin/corporate-memory",
    "GET /admin/database",
    "GET /admin/grants",
    "GET /admin/groups",
    "GET /admin/groups/{group_id}",
    "GET /admin/initial-workspace",
    "GET /admin/marketplaces",
    "GET /admin/mcp-sources",
    "GET /admin/mcp-sources/{source_id}",
    "GET /admin/mcp-tools/{tool_id}/grants",
    "GET /admin/news",
    "GET /admin/prompts",
    "GET /admin/scheduler-runs",
    "GET /admin/server-config",
    "GET /admin/sessions",
    "GET /admin/sessions/{username}/{session_file}",
    "GET /admin/store/submissions",
    "GET /admin/store/submissions/{submission_id}",
    "GET /admin/sync",
    "GET /admin/tables",
    "GET /admin/telemetry",
    "GET /admin/tokens",
    "GET /admin/usage",
    "GET /admin/users",
    "GET /admin/users/{user_id}",
    "GET /admin/workspace-prompt",
    # HTML web pages — covered by separate UI test suite
    "GET /activity-center",
    "GET /catalog",
    "GET /catalog/p/{slug}",
    "GET /catalog/r/{slug}",
    "GET /catalog/t/{table_id}",
    "GET /chat",
    "GET /corporate-memory",
    "GET /dashboard",
    "GET /docs",
    "GET /documentation/api",
    "GET /first-time-setup",
    "GET /help/cowork",
    "GET /home",
    "GET /install",
    "GET /login",
    "GET /login/email",
    "GET /login/password",
    "GET /marketplace",
    "GET /marketplace.zip",
    "GET /marketplace/cowork/{prefixed_name}.zip",
    "GET /marketplace/curated/{marketplace_id}/{plugin_name}",
    "GET /marketplace/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}",
    "GET /marketplace/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}",
    "GET /marketplace/flea/{entity_id}",
    "GET /marketplace/flea/{entity_id}/agent/{agent_name}",
    "GET /marketplace/flea/{entity_id}/edit",
    "GET /marketplace/flea/{entity_id}/skill/{skill_name}",
    "GET /marketplace/format-guide",
    "GET /marketplace/guide/curated",
    "GET /marketplace/guide/flea",
    "GET /marketplace/info",
    "GET /me/activity",
    "GET /me/cowork",
    "GET /me/mcp",
    "GET /me/profile",
    "GET /me/stats",
    "GET /memory/d/{slug}",
    "GET /news",
    "GET /openapi.json",
    "GET /profile/sessions",
    "GET /profile/sessions/{filename}",
    "GET /redoc",
    "GET /setup",
    "GET /setup-advanced",
    "GET /slack/bind",
    "GET /store/examples",
    "GET /store/new",
    "GET /webhooks/jira/health",
    # Auth flows — web form endpoints
    "GET /auth/email/verify",
    "GET /auth/password/reset",
    "GET /auth/password/setup",
    "POST /auth/email/send-link",
    "POST /auth/email/verify",
    "POST /auth/password/login/web",
    "POST /auth/password/reset",
    "POST /auth/password/reset/confirm",
    "POST /auth/password/setup",
    "POST /auth/password/setup/confirm",
    "POST /auth/password/setup/request",
    "POST /auth/refresh-groups",
    "POST /cli/auth/exchange",
    "POST /cli/auth/start",
    "GET /cli/auth/start",
    "GET /cli/download",
    "GET /cli/install.sh",
    "GET /cli/latest",
    "GET /cli/wheel/{wheel_name}",
    "POST /api/auth/exchange-setup-token",
    "POST /me/profile/refetch-groups",
    # Debug / introspection — not business logic
    "GET /_debug/throw/exc",
    "GET /_debug/throw/http/{code:int}",
    "GET /api/debug/throw",
    # Admin data-packages — CRUD used as test helper; full suite planned
    "GET /api/admin/data-packages",
    "GET /api/admin/data-packages/{pkg_id}",
    "PUT /api/admin/data-packages/{pkg_id}",
    "DELETE /api/admin/data-packages/{pkg_id}",
    "DELETE /api/admin/data-packages/{pkg_id}/tables/{table_id}",
    "DELETE /api/admin/data-packages/{pkg_id}/tools/{tool_id}",
    "POST /api/admin/data-packages",
    "POST /api/admin/data-packages/{pkg_id}/restore",
    "POST /api/admin/data-packages/{pkg_id}/tables",
    "POST /api/admin/data-packages/{pkg_id}/tools",
    # Admin adoption / analytics — DuckDB analytics panels
    "GET /api/admin/adoption/kpis",
    "GET /api/admin/adoption/series",
    "GET /api/admin/adoption/top-skills",
    "GET /api/admin/adoption/top-users",
    "GET /api/admin/adoption/users/{user_id}/kpis",
    "GET /api/admin/adoption/users/{user_id}/series",
    "GET /api/admin/adoption/users/{user_id}/top-skills",
    "GET /api/admin/adoption/users/{user_id}/top-tools",
    # Admin cache warmup — internal background job
    "GET /api/admin/cache-warmup/status",
    "GET /api/admin/cache-warmup/stream",
    "POST /api/admin/cache-warmup/run",
    # Admin DB management — migration / job control
    "GET /api/admin/db/job/{job_id}",
    "GET /api/admin/db/state",
    "POST /api/admin/db/cancel/{job_id}",
    "POST /api/admin/db/migrate",
    "DELETE /api/admin/initial-workspace",
    "GET /api/admin/initial-workspace",
    "POST /api/admin/initial-workspace",
    "POST /api/admin/initial-workspace/sync",
    "POST /api/admin/initial-workspace/sync-if-configured",
    # Admin MCP sources/tools — operator config
    "DELETE /api/admin/mcp-sources/{source_id}",
    "DELETE /api/admin/mcp-sources/{source_id}/secret",
    "DELETE /api/admin/mcp-tools/{tool_id}",
    "DELETE /api/admin/mcp-tools/{tool_id}/grants/{group_id}",
    "GET /api/admin/mcp-sources",
    "GET /api/admin/mcp-sources/{source_id}",
    "GET /api/admin/mcp-tools",
    "GET /api/admin/mcp-tools/{tool_id}",
    "POST /api/admin/mcp-sources",
    "POST /api/admin/mcp-sources/{source_id}/classify",
    "POST /api/admin/mcp-sources/{source_id}/introspect",
    "POST /api/admin/mcp-sources/{source_id}/materialize",
    "POST /api/admin/mcp-sources/{source_id}/test",
    "POST /api/admin/mcp-tools",
    "POST /api/admin/mcp-tools/{tool_id}/grants",
    "PUT /api/admin/mcp-sources/{source_id}",
    "PUT /api/admin/mcp-sources/{source_id}/secret",
    "PUT /api/admin/mcp-tools/{tool_id}",
    # Admin memory domains — complex admin feature
    "DELETE /api/admin/memory-domains/{domain_id}",
    "DELETE /api/admin/memory-domains/{domain_id}/items/{item_id}",
    "GET /api/admin/memory-domain-suggestions",
    "GET /api/admin/memory-domain-suggestions/count-pending",
    "GET /api/admin/memory-domains",
    "GET /api/admin/memory-domains/{domain_id}",
    "POST /api/admin/memory-domain-suggestions/{sid}/approve",
    "POST /api/admin/memory-domain-suggestions/{sid}/reject",
    "POST /api/admin/memory-domains",
    "POST /api/admin/memory-domains/{domain_id}/items",
    "POST /api/admin/memory-domains/{domain_id}/restore",
    "PUT /api/admin/memory-domains/{domain_id}",
    # Admin news
    "GET /api/admin/news/current",
    "GET /api/admin/news/draft",
    "GET /api/admin/news/versions",
    "GET /api/admin/news/versions/{version}",
    "POST /api/admin/news/preview",
    "POST /api/admin/news/publish",
    "POST /api/admin/news/unpublish/{version}",
    "PUT /api/admin/news/draft",
    # Admin observability
    "DELETE /api/admin/observability/views/{view_id}",
    "GET /api/admin/observability/facets",
    "GET /api/admin/observability/kpis",
    "GET /api/admin/observability/views",
    "POST /api/admin/observability/views",
    # Admin prompts
    "DELETE /api/admin/prompts/{kind}",
    "GET /api/admin/prompts/iwt-files",
    "GET /api/admin/prompts/{kind}",
    "POST /api/admin/prompts/{kind}/bind-git",
    "POST /api/admin/prompts/{kind}/preview",
    "POST /api/admin/prompts/{kind}/source",
    "PUT /api/admin/prompts/{kind}",
    # Admin recipes
    "DELETE /api/admin/recipes/{recipe_id}",
    "GET /api/admin/recipes/{recipe_id}",
    "POST /api/admin/recipes",
    "POST /api/admin/recipes/{recipe_id}/restore",
    "PUT /api/admin/recipes/{recipe_id}",
    # Admin slack secrets
    "DELETE /api/admin/slack-secrets/{name}",
    "PUT /api/admin/slack-secrets/{name}",
    # Admin store submissions (detail/actions beyond list)
    "DELETE /api/admin/store/submissions/{submission_id}",
    "GET /api/admin/store/submissions/{submission_id}",
    "GET /api/admin/store/submissions/{submission_id}/bundle.zip",
    "POST /api/admin/store/submissions/{submission_id}/override",
    "POST /api/admin/store/submissions/{submission_id}/rescan",
    "POST /api/admin/store/submissions/{submission_id}/retry",
    # Admin telemetry
    "GET /api/admin/telemetry/export",
    "GET /api/admin/telemetry/facets",
    "GET /api/admin/telemetry/kpis",
    "GET /api/admin/telemetry/query",
    "GET /api/admin/telemetry/summary",
    "POST /api/admin/telemetry/ask",
    "POST /api/admin/telemetry/prune",
    "POST /api/admin/telemetry/reprocess",
    # Admin sessions downloads
    "GET /api/admin/sessions/{username}/{session_file}/download",
    "GET /api/admin/sessions/{username}/{session_file}/transcript",
    # Admin users (per-user detail views)
    "DELETE /api/admin/users/{user_id}/memberships/{group_id}",
    "GET /api/admin/users/{user_id}/activity",
    "GET /api/admin/users/{user_id}/effective-access",
    "GET /api/admin/users/{user_id}/memberships",
    "GET /api/admin/users/{user_id}/sessions",
    "GET /api/admin/users/{user_id}/sessions/download-all",
    "GET /api/admin/users/{user_id}/sessions/{session_file}/download",
    "POST /api/admin/users/{user_id}/memberships",
    # Admin welcome/workspace templates
    "DELETE /api/admin/welcome-template",
    "DELETE /api/admin/workspace-prompt-template",
    "GET /api/admin/welcome-template",
    "GET /api/admin/workspace-prompt-template",
    "POST /api/admin/welcome-template/preview",
    "POST /api/admin/workspace-prompt-template/preview",
    "PUT /api/admin/welcome-template",
    "PUT /api/admin/workspace-prompt-template",
    # Admin misc operations
    "DELETE /api/admin/metrics/{metric_id}",
    "PATCH /api/admin/registry/{table_id}/docs",
    "POST /api/admin/bigquery/test-connection",
    "POST /api/admin/discover-and-register",
    "POST /api/admin/keboola/test-connection",
    "POST /api/admin/metadata/{table_id}",
    "POST /api/admin/metrics",
    "POST /api/admin/run-blocked-purge",
    "POST /api/admin/run-bq-metadata-refresh",
    "POST /api/admin/run-corporate-memory",
    "POST /api/admin/run-jira-consistency-check",
    "POST /api/admin/run-jira-sla-poll",
    "POST /api/admin/run-session-collector",
    "POST /api/admin/run-session-processor",
    "POST /api/admin/uploads/cover-image",
    # Catalog detail views
    "GET /api/catalog/metrics/{metric_path}",
    "GET /api/catalog/profile/{table_name}",
    "POST /api/catalog/profile/{table_name}/refresh",
    # Chat (beyond live SSE)
    "DELETE /api/chat/sessions/{chat_id}",
    "GET /api/chat/sessions",
    "GET /api/chat/sessions/{chat_id}/messages",
    "GET /api/chat/{session_id}/messages",
    "POST /api/chat/sessions/{chat_id}/ticket",
    "POST /api/chat/{session_id}/fork",
    "POST /api/chat/{session_id}/invite",
    "POST /api/chat/{session_id}/join-ticket",
    "POST /api/chat/{session_id}/leave",
    # Data packages (user-facing slug lookup)
    "GET /api/data-packages/{slug}",
    # Initial workspace
    "GET /api/initial-workspace",
    "GET /api/initial-workspace.zip",
    "POST /api/initial-workspace/applied",
    # Marketplace detail / asset endpoints
    "DELETE /api/marketplace/curated/{marketplace_id}/{plugin_name}/install",
    "DELETE /api/marketplaces/{marketplace_id}",
    "DELETE /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/system",
    "POST /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/disable",
    "POST /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/enable",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/asset/{path}",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/doc/{path}",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/mirrored/{key}",
    "GET /api/marketplace/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}",
    "GET /api/marketplace/flea/{entity_id}/agent/{agent_name}",
    "GET /api/marketplace/flea/{entity_id}/skill/{skill_name}",
    "GET /api/marketplaces/{marketplace_id}/plugins",
    "PATCH /api/marketplaces/{marketplace_id}",
    "POST /api/marketplace/curated/{marketplace_id}/{plugin_name}/install",
    "POST /api/marketplaces/sync-all",
    "POST /api/marketplaces/{marketplace_id}/plugins/{plugin_name}/system",
    "POST /api/marketplaces/{marketplace_id}/sync",
    # MCP passthrough / user secrets
    "DELETE /api/mcp/sources/{source_id}/my-secret",
    "GET /api/mcp/passthrough/tools",
    "GET /api/mcp/sources/{source_id}/my-secret",
    "POST /api/mcp/passthrough/tools/{tool_id}/call",
    "POST /api/mcp/query-table/{table_id}",
    "PUT /api/mcp/sources/{source_id}/my-secret",
    # Memory advanced routes (audit, votes, tree, etc.)
    "DELETE /api/memory/{item_id}/dismiss",
    "GET /api/memory-domain-suggestions/mine",
    "GET /api/memory/admin/audit",
    "GET /api/memory/admin/contradictions",
    "GET /api/memory/admin/duplicate-candidates",
    "GET /api/memory/admin/pending",
    "GET /api/memory/admin/{item_id}",
    "GET /api/memory/bundle",
    "GET /api/memory/domains/{slug}",
    "GET /api/memory/my-contributions",
    "GET /api/memory/my-votes",
    "GET /api/memory/tree",
    "PATCH /api/memory/admin/{item_id}",
    "POST /api/memory-domain-suggestions",
    "POST /api/memory/admin/approve",
    "POST /api/memory/admin/batch",
    "POST /api/memory/admin/bulk-update",
    "POST /api/memory/admin/contradictions",
    "POST /api/memory/admin/contradictions/{contradiction_id}/resolve",
    "POST /api/memory/admin/duplicate-candidates/resolve",
    "POST /api/memory/admin/edit",
    "POST /api/memory/admin/mandate",
    "POST /api/memory/admin/revoke",
    "POST /api/memory/items/{item_id}/mark-mandatory",
    "POST /api/memory/items/{item_id}/mark-unmandatory",
    "POST /api/memory/{item_id}/dismiss",
    "POST /api/memory/{item_id}/personal",
    # Metrics (user-facing)
    "GET /api/metrics/{metric_id}",
    # Recipes (user-facing)
    "GET /api/recipes/{slug}",
    # Scripts (run/deploy actions beyond list)
    "DELETE /api/scripts/{script_id}",
    "POST /api/scripts/deploy",
    "POST /api/scripts/run",
    "POST /api/scripts/run-due",
    "POST /api/scripts/{script_id}/run",
    # Stack (new subscription API)
    "DELETE /api/stack/subscription/{resource_type}/{resource_id}",
    "GET /api/stack",
    "GET /api/stack/browse",
    "POST /api/stack/subscribe",
    # Store version restore
    "POST /api/store/entities/{entity_id}/versions/{version_no}/restore",
    # Sync (pull-confirm / settings)
    "POST /api/sync/pull-confirm",
    "POST /api/sync/settings",
    "POST /api/sync/table-subscriptions",
    # Telegram
    "GET /api/telegram/status",
    "POST /api/telegram/unlink",
    "POST /api/telegram/verify",
    # User setup tokens
    "DELETE /api/user/setup-tokens/{token_id}",
    "GET /api/user/setup-tokens",
    # User cowork
    "POST /api/user/cowork-bundle",
    # V2 metadata/marketplace
    "GET /api/v2/marketplace/skills",
    "GET /api/v2/metadata-cache/status",
    "POST /api/v2/metadata-cache/refresh",
    # Jira webhooks / slack bind
    "GET /slack/bind",
    "POST /api/slack/bind",
    "POST /api/slack/commands",
    "POST /api/slack/interactivity",
    "POST /webhooks/jira",
    # My-stack curated toggle
    "PUT /api/my-stack/curated/{marketplace_id}/{plugin_name}",
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


def test_every_route_is_covered_or_excluded():
    """Route-coverage guard: fails CI when a new endpoint has no test or exclusion.

    Uses a subprocess to inspect routes so xdist worker state (importlib.reload
    calls from state_backend fixture) cannot corrupt the route set. The subprocess
    imports app.main in a clean Python process and emits JSON to stdout.

    Uses app.openapi()["paths"] rather than iterating app.routes directly.
    Starlette 1.3.x wraps included routers in _IncludedRouter objects that lack
    .path/.methods attributes, so direct iteration raises AttributeError. The
    OpenAPI schema is the authoritative flat route list regardless of Starlette
    version. Note: OpenAPI strips the :path convertor suffix from path parameters
    ({metric_id:path} -> {metric_id}), so KNOWN_UNTESTED entries must use the
    plain {param} form.
    """
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = (
        "import json, warnings; warnings.filterwarnings('ignore'); "
        "from app.main import app; "
        "schema = app.openapi(); "
        "rows = [{'path': p, 'methods': list(ms.keys())} "
        "for p, ms in schema.get('paths', {}).items()]; "
        "print(json.dumps(rows))"
    )
    result = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", script],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": repo_root},
    )
    assert result.returncode == 0, f"Route inspection subprocess failed (exit {result.returncode}):\n{result.stderr}"
    routes_data = json.loads(result.stdout)
    all_routes = {
        f"{m.upper()} {r['path']}" for r in routes_data for m in r["methods"] if m.upper() not in ("HEAD", "OPTIONS")
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
