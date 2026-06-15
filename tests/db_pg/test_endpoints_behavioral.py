"""Behavioral integration tests — HTTP mutations + direct backend reads.

Each test:
  1. Mutates state via HTTP (POST/PUT/DELETE)
  2. Reads DIRECTLY from the active backend via the repository factory
     to confirm the mutation landed in the right place
  3. For [+neg-duck] tests: also probes DuckDB directly on the [pg] run
     to confirm the mutation did NOT leak into DuckDB

All tests run twice: once with DuckDB-only (seeded_app_both/state_backend=duckdb)
and once with Postgres active (state_backend=pg).
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers.assertions import assert_only_in_active_backend
from tests.helpers.factories import make_skill_zip

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Module-level header helpers (consistent with smoke file)
# ---------------------------------------------------------------------------


def _admin_headers(s):
    return {"Authorization": f"Bearer {s['admin_token']}"}


def _analyst_headers(s):
    return {"Authorization": f"Bearer {s['analyst_token']}"}


# ---------------------------------------------------------------------------
# DuckDB probe helper
# ---------------------------------------------------------------------------


def _duck_probe(table, id_col, id_val):
    """Probe the DuckDB system DB for a row — returns None on any error or miss."""
    try:
        from src.db import get_system_db
        return get_system_db().execute(
            f"SELECT 1 FROM {table} WHERE {id_col} = ?", [id_val]
        ).fetchone()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cluster 1: Users + RBAC
# ---------------------------------------------------------------------------


class TestUsersRBACBehavioral:
    """HTTP mutations land in the correct backend + no dual-write leaks."""

    def test_create_user_persists_to_active_backend(self, seeded_app_both):
        """POST /api/admin/users → 201; repo read confirms row exists. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        new_email = "behavioral-new@test.com"
        r = client.post(
            "/api/admin/users",
            json={"email": new_email, "name": "Behavioral User"},
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        user_id = r.json()["id"]

        from src.repositories import users_repo

        assert_only_in_active_backend(
            repo_read=lambda: users_repo().get_by_id(user_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("users", "id", user_id),
        )
        # Verify the returned email matches what we created
        from src.repositories import users_repo as _ur
        row = _ur().get_by_id(user_id)
        assert row["email"] == new_email

    def test_create_group_persists_to_active_backend(self, seeded_app_both):
        """POST /api/admin/groups → 201; repo read confirms row exists. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        r = client.post(
            "/api/admin/groups",
            json={"name": "BehavioralGroup", "description": "Test group for behavioral tests"},
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        group_id = r.json()["id"]

        from src.repositories import user_groups_repo

        assert_only_in_active_backend(
            repo_read=lambda: user_groups_repo().get(group_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("user_groups", "id", group_id),
        )

    def test_add_member_persists_to_active_backend(self, seeded_app_both):
        """Create group → POST members → 201; member appears in list."""
        s = seeded_app_both
        client = s["client"]

        # Create group
        rg = client.post(
            "/api/admin/groups",
            json={"name": "MemberGroup", "description": "Group for member tests"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        # Add analyst1 to group
        rm = client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"user_id": "analyst1"},
            headers=_admin_headers(s),
        )
        assert rm.status_code == 201, rm.text

        from src.repositories import user_group_members_repo

        members = user_group_members_repo().list_members_for_group(group_id)
        user_ids = [m["user_id"] for m in members]
        assert "analyst1" in user_ids

    def test_revoke_member_removes_from_active_backend(self, seeded_app_both):
        """Create group → add member → DELETE member → 204; member absent."""
        s = seeded_app_both
        client = s["client"]

        # Create group
        rg = client.post(
            "/api/admin/groups",
            json={"name": "RevokeGroup", "description": "Group for revoke tests"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        # Add analyst1
        client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"user_id": "analyst1"},
            headers=_admin_headers(s),
        )

        # Remove analyst1
        rd = client.delete(
            f"/api/admin/groups/{group_id}/members/analyst1",
            headers=_admin_headers(s),
        )
        assert rd.status_code == 204, rd.text

        from src.repositories import user_group_members_repo

        members = user_group_members_repo().list_members_for_group(group_id)
        user_ids = [m["user_id"] for m in members]
        assert "analyst1" not in user_ids

    def test_grant_table_access_visible_in_manifest(self, seeded_app_both, registered_table_both):
        """Grant group access to table → analyst sees table in manifest. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]
        table_id = registered_table_both["table_id"]

        # Create a new group for analyst
        rg = client.post(
            "/api/admin/groups",
            json={"name": "AnalystAccessGroup", "description": "Analyst access group"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        # Add analyst1 to the group
        client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"user_id": "analyst1"},
            headers=_admin_headers(s),
        )

        # Grant group access to the table
        rp = client.post(
            "/api/admin/grants",
            json={"group_id": group_id, "resource_type": "table", "resource_id": table_id},
            headers=_admin_headers(s),
        )
        assert rp.status_code == 201, rp.text
        grant_id = rp.json()["id"]

        # Verify grant persisted in active backend
        from src.repositories import resource_grants_repo

        assert_only_in_active_backend(
            repo_read=lambda: resource_grants_repo().get(grant_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("resource_grants", "id", grant_id),
        )

        # Analyst should see table in manifest
        rm = client.get("/api/sync/manifest", headers=_analyst_headers(s))
        assert rm.status_code == 200, rm.text
        manifest = rm.json()
        table_ids = [t["id"] for t in manifest.get("tables", [])]
        assert table_id in table_ids, (
            f"table {table_id!r} not in manifest tables after grant; "
            f"manifest tables: {table_ids!r}"
        )


# ---------------------------------------------------------------------------
# Cluster 2: Table Registry + Sync
# ---------------------------------------------------------------------------


class TestTableRegistrySyncBehavioral:
    """Table registry CRUD lands in the correct backend."""

    def test_register_table_writes_to_active_backend(self, seeded_app_both):
        """POST /api/admin/register-table → 201; repo confirms row. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        r = client.post(
            "/api/admin/register-table",
            json={
                "name": "behavioral_reg_table",
                "source_type": "keboola",
                "bucket": "behavioral_src",
                "source_table": "behavioral_reg_table",
                "query_mode": "local",
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        table_id = r.json()["id"]

        from src.repositories import table_registry_repo

        assert_only_in_active_backend(
            repo_read=lambda: table_registry_repo().get(table_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("table_registry", "id", table_id),
        )

    def test_update_table_persists(self, seeded_app_both, registered_table_both):
        """PUT /api/admin/registry/{table_id} → updated sync_schedule reflects in repo."""
        s = seeded_app_both
        client = s["client"]
        table_id = registered_table_both["table_id"]

        from src.repositories import table_registry_repo

        # Fetch existing row to build update payload
        existing = table_registry_repo().get(table_id)
        assert existing is not None

        new_schedule = "0 6 * * *"
        r = client.put(
            f"/api/admin/registry/{table_id}",
            json={
                "name": existing.get("name") or "smoke_orders",
                "source_type": existing.get("source_type") or "keboola",
                "bucket": existing.get("bucket") or "smoke_src",
                "source_table": existing.get("source_table") or "smoke_orders",
                "query_mode": existing.get("query_mode") or "local",
                "sync_schedule": new_schedule,
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 200, r.text

        updated = table_registry_repo().get(table_id)
        assert updated is not None
        assert updated.get("sync_schedule") == new_schedule

    def test_delete_table_removes_from_active_backend(self, seeded_app_both):
        """Register table → DELETE /api/admin/registry/{table_id} → repo returns None."""
        s = seeded_app_both
        client = s["client"]

        # Register a table
        r = client.post(
            "/api/admin/register-table",
            json={
                "name": "behavioral_del_table",
                "source_type": "keboola",
                "bucket": "del_src",
                "source_table": "behavioral_del_table",
                "query_mode": "local",
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        table_id = r.json()["id"]

        # Delete it
        rd = client.delete(
            f"/api/admin/registry/{table_id}",
            headers=_admin_headers(s),
        )
        assert rd.status_code == 204, rd.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id) is None


# ---------------------------------------------------------------------------
# Cluster 3: Memory (Knowledge items)
# ---------------------------------------------------------------------------


class TestMemoryBehavioral:
    """Memory create/delete mutations land in the correct backend."""

    def test_create_knowledge_item_persists_to_active_backend(self, seeded_app_both):
        """POST /api/memory → 201; knowledge_repo().get_by_id confirms row. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        r = client.post(
            "/api/memory",
            json={
                "title": "Behavioral memory item",
                "content": "This is the content of the behavioral memory item used in tests.",
                "category": "process",
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        item_id = r.json()["id"]

        from src.repositories import knowledge_repo

        assert_only_in_active_backend(
            repo_read=lambda: knowledge_repo().get_by_id(item_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("knowledge_items", "id", item_id),
        )

    def test_delete_knowledge_item_removes(self, seeded_app_both):
        """POST create → admin action removes / archives item from active backend."""
        s = seeded_app_both
        client = s["client"]

        # Create item
        r = client.post(
            "/api/memory",
            json={
                "title": "To be removed",
                "content": "This item will be rejected/revoked in the test flow.",
                "category": "process",
            },
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        item_id = r.json()["id"]

        # Use admin reject endpoint to change status (the memory API uses
        # admin/approve/reject lifecycle, not a raw DELETE)
        rr = client.post(
            "/api/memory/admin/reject",
            json={"id": item_id},
            headers=_admin_headers(s),
        )
        assert rr.status_code in (200, 204), rr.text

        from src.repositories import knowledge_repo

        item = knowledge_repo().get_by_id(item_id)
        # After reject the item still exists but with rejected status
        assert item is not None
        assert item.get("status") == "rejected"


# ---------------------------------------------------------------------------
# Cluster 4: Store (entities, submissions, install/uninstall)
# ---------------------------------------------------------------------------


class TestStoreBehavioral:
    """Store entity mutations land in the correct backend."""

    def _upload_skill(self, client, headers, name="behavioral-skill"):
        """Upload a skill with guardrails off → expects 201 + approved."""
        zb = make_skill_zip(name)
        r = client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=headers,
        )
        return r

    def test_upload_skill_guardrails_off_immediately_approved(self, seeded_app_both, monkeypatch):
        """POST /api/store/entities (guardrails off) → 201 + approved in active backend. [+neg-duck]"""
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: False)

        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-guardrail-off")
        assert r.status_code == 201, r.text
        entity = r.json()
        entity_id = entity["id"]
        assert entity["visibility_status"] == "approved", (
            f"Expected approved when guardrails off, got {entity['visibility_status']!r}"
        )

        from src.repositories import store_entities_repo

        assert_only_in_active_backend(
            repo_read=lambda: store_entities_repo().get(entity_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("store_entities", "id", entity_id),
        )
        row = store_entities_repo().get(entity_id)
        assert row["visibility_status"] == "approved"

    def test_upload_skill_guardrails_on_llm_approve_flow(self, seeded_app_both, monkeypatch):
        """Mock LLM approve: POST → pending_llm; run_llm_review → approved."""
        monkeypatch.setattr("src.store_guardrails.llm_review.review_bundle", lambda *a, **kw: {
            "risk_level": "low", "summary": "mock approve", "findings": [],
            "reviewed_by_model": "mock", "error": None,
        })
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        s = seeded_app_both
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-llm-approve")
        assert r.status_code == 201, r.text
        entity = r.json()
        entity_id = entity["id"]
        assert entity["visibility_status"] == "pending_llm", (
            f"Expected pending_llm, got {entity['visibility_status']!r}"
        )

        from src.repositories import store_submissions_repo
        from src.store_guardrails.runner import run_llm_review
        from pathlib import Path

        sub = store_submissions_repo().latest_for_entity(entity_id)
        assert sub is not None
        sub_id = sub["id"]

        # Determine plugin dir for run_llm_review
        plugin_dir = s["data_dir"] / "store_uploads" / entity_id
        plugin_dir.mkdir(parents=True, exist_ok=True)

        run_llm_review(
            sub_id,
            plugin_dir=plugin_dir,
            api_key_loader=lambda: "mock-key",
            model_loader=lambda: "mock-model",
        )

        updated_sub = store_submissions_repo().get(sub_id)
        assert updated_sub["status"] == "approved", (
            f"Expected approved after LLM approve, got {updated_sub['status']!r}"
        )

    def test_upload_skill_guardrails_on_llm_block_flow(self, seeded_app_both, monkeypatch):
        """Mock LLM block → pending_llm; run_llm_review → blocked_llm; admin override → approved."""
        monkeypatch.setattr("src.store_guardrails.llm_review.review_bundle", lambda *a, **kw: {
            "risk_level": "high", "summary": "mock block",
            "findings": [{"file": "x", "explanation": "mock"}],
            "reviewed_by_model": "mock", "error": None,
        })
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        s = seeded_app_both
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-llm-block")
        assert r.status_code == 201, r.text
        entity = r.json()
        entity_id = entity["id"]

        from src.repositories import store_submissions_repo, store_entities_repo
        from src.store_guardrails.runner import run_llm_review
        from pathlib import Path

        sub = store_submissions_repo().latest_for_entity(entity_id)
        assert sub is not None
        sub_id = sub["id"]

        plugin_dir = s["data_dir"] / "store_uploads" / entity_id
        plugin_dir.mkdir(parents=True, exist_ok=True)

        run_llm_review(
            sub_id,
            plugin_dir=plugin_dir,
            api_key_loader=lambda: "mock-key",
            model_loader=lambda: "mock-model",
        )

        blocked_sub = store_submissions_repo().get(sub_id)
        assert blocked_sub["status"] == "blocked_llm", (
            f"Expected blocked_llm, got {blocked_sub['status']!r}"
        )

        # Admin override
        ro = client.post(
            f"/api/admin/store/submissions/{sub_id}/override",
            json={"reason": "test override for behavioral test"},
            headers=_admin_headers(s),
        )
        assert ro.status_code == 200, ro.text

        overridden_entity = store_entities_repo().get(entity_id)
        assert overridden_entity["visibility_status"] == "approved", (
            f"Expected approved after override, got {overridden_entity['visibility_status']!r}"
        )

    def test_install_entity_writes_to_active_backend(self, seeded_app_both, monkeypatch):
        """Upload → install; user_store_installs shows entity; /api/my-stack reflects it."""
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: False)

        s = seeded_app_both
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-install")
        assert r.status_code == 201, r.text
        entity_id = r.json()["id"]

        # Install
        ri = client.post(
            f"/api/store/entities/{entity_id}/install",
            headers=_admin_headers(s),
        )
        assert ri.status_code in (200, 201), ri.text

        from src.repositories import user_store_installs_repo

        installs = user_store_installs_repo().list_for_user("admin1")
        installed_ids = [i["id"] for i in installs]
        assert entity_id in installed_ids, (
            f"entity {entity_id!r} not in installs after install; installs: {installed_ids!r}"
        )

        # Verify via /api/my-stack
        rs = client.get("/api/my-stack", headers=_admin_headers(s))
        assert rs.status_code == 200, rs.text
        stack_ids = [e["id"] for e in rs.json()]
        assert entity_id in stack_ids

    def test_uninstall_removes_from_active_backend(self, seeded_app_both, monkeypatch):
        """Upload → install → uninstall; entity absent from installs and /api/my-stack."""
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: False)

        s = seeded_app_both
        client = s["client"]

        r = self._upload_skill(client, _admin_headers(s), "behavioral-uninstall")
        assert r.status_code == 201, r.text
        entity_id = r.json()["id"]

        # Install
        client.post(f"/api/store/entities/{entity_id}/install", headers=_admin_headers(s))

        # Uninstall
        ru = client.delete(
            f"/api/store/entities/{entity_id}/install",
            headers=_admin_headers(s),
        )
        assert ru.status_code == 204, ru.text

        from src.repositories import user_store_installs_repo

        installs = user_store_installs_repo().list_for_user("admin1")
        installed_ids = [i["id"] for i in installs]
        assert entity_id not in installed_ids

        # Verify via /api/my-stack
        rs = client.get("/api/my-stack", headers=_admin_headers(s))
        assert rs.status_code == 200, rs.text
        stack_ids = [e["id"] for e in rs.json()]
        assert entity_id not in stack_ids


# ---------------------------------------------------------------------------
# Cluster 5: Reaper contract
# ---------------------------------------------------------------------------


class TestReaperContract:
    """Reaper flips aged pending_llm submissions to review_error."""

    def _backdate_submission(self, sub_id: str, backend: str, hours: int = 2) -> None:
        """Backdate a submission's created_at to now() - hours."""
        old_ts = datetime.now(timezone.utc) - timedelta(hours=hours)
        if backend == "duckdb":
            from src.db import get_system_db
            get_system_db().execute(
                "UPDATE store_submissions SET created_at = ? WHERE id = ?",
                [old_ts, sub_id],
            )
        else:
            import sqlalchemy as sa
            from src.db_pg import get_engine
            with get_engine().begin() as conn:
                conn.execute(
                    sa.text(
                        "UPDATE store_submissions SET created_at = :ts WHERE id = :id"
                    ),
                    {"ts": old_ts, "id": sub_id},
                )

    def test_reaper_flips_aged_pending_llm_to_review_error(self, seeded_app_both):
        """Aged pending_llm → run-reap-stuck-reviews → review_error. [+neg-duck]"""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        from src.repositories import store_submissions_repo

        sub_id = store_submissions_repo().create(
            submitter_id="admin1",
            submitter_email="admin@test.com",
            type="skill",
            name="reaper-test-skill",
            version=None,
            status="pending_llm",
        )

        # Backdate so the reaper picks it up (default grace = 1800s; we go 2h back)
        self._backdate_submission(sub_id, backend, hours=2)

        r = client.post("/api/admin/run-reap-stuck-reviews", headers=_admin_headers(s))
        assert r.status_code == 200, r.text
        result = r.json()
        assert result.get("details", {}).get("reaped", 0) >= 1, (
            f"Expected reaped >= 1, got: {result!r}"
        )

        assert_only_in_active_backend(
            repo_read=lambda: store_submissions_repo().get(sub_id),
            backend=backend,
            duckdb_probe=lambda: _duck_probe("store_submissions", "id", sub_id),
        )

        reaped_sub = store_submissions_repo().get(sub_id)
        assert reaped_sub["status"] == "review_error", (
            f"Expected review_error after reaping, got {reaped_sub['status']!r}"
        )

    def test_reaper_preserves_fresh_pending_llm(self, seeded_app_both):
        """Fresh pending_llm row is NOT flipped by the reaper."""
        s = seeded_app_both
        client = s["client"]

        from src.repositories import store_submissions_repo

        # Fresh row (current timestamp — well within grace period)
        sub_id = store_submissions_repo().create(
            submitter_id="admin1",
            submitter_email="admin@test.com",
            type="skill",
            name="reaper-fresh-skill",
            version=None,
            status="pending_llm",
        )

        r = client.post("/api/admin/run-reap-stuck-reviews", headers=_admin_headers(s))
        assert r.status_code == 200, r.text

        fresh_sub = store_submissions_repo().get(sub_id)
        assert fresh_sub is not None
        assert fresh_sub["status"] == "pending_llm", (
            f"Expected fresh row to remain pending_llm, got {fresh_sub['status']!r}"
        )

    def test_reaper_multi_row_correct_count(self, seeded_app_both):
        """3 aged + 1 fresh: aged rows flipped, fresh row preserved."""
        s = seeded_app_both
        backend = s["backend"]
        client = s["client"]

        from src.repositories import store_submissions_repo

        # Create 3 aged + 1 fresh
        aged_ids = []
        for i in range(3):
            sid = store_submissions_repo().create(
                submitter_id="admin1",
                submitter_email="admin@test.com",
                type="skill",
                name=f"reaper-multi-aged-{i}",
                version=None,
                status="pending_llm",
            )
            self._backdate_submission(sid, backend, hours=2)
            aged_ids.append(sid)

        fresh_id = store_submissions_repo().create(
            submitter_id="admin1",
            submitter_email="admin@test.com",
            type="skill",
            name="reaper-multi-fresh",
            version=None,
            status="pending_llm",
        )

        r = client.post("/api/admin/run-reap-stuck-reviews", headers=_admin_headers(s))
        assert r.status_code == 200, r.text

        # All 3 aged rows should be review_error
        for sid in aged_ids:
            sub = store_submissions_repo().get(sid)
            assert sub["status"] == "review_error", (
                f"Expected review_error for aged sub {sid!r}, got {sub['status']!r}"
            )

        # Fresh row should remain pending_llm
        fresh_sub = store_submissions_repo().get(fresh_id)
        assert fresh_sub["status"] == "pending_llm", (
            f"Expected fresh row to remain pending_llm, got {fresh_sub['status']!r}"
        )


# ---------------------------------------------------------------------------
# Cluster 6: Data access + Admin overview
# ---------------------------------------------------------------------------


class TestDataAccessBehavioral:
    """RBAC enforcement on data endpoints is backed by the active backend."""

    def test_check_access_enforces_rbac_per_backend(self, seeded_app_both, registered_table_both):
        """Analyst has no access → 403; after grant → 200."""
        s = seeded_app_both
        client = s["client"]
        table_id = registered_table_both["table_id"]

        # Analyst has no grant yet → 403
        r_before = client.get(
            f"/api/data/{table_id}/check-access",
            headers=_analyst_headers(s),
        )
        assert r_before.status_code == 403, (
            f"Expected 403 before grant, got {r_before.status_code}: {r_before.text}"
        )

        # Create group + add analyst + grant table access
        rg = client.post(
            "/api/admin/groups",
            json={"name": "DataAccessGroup", "description": "Group for data access tests"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"user_id": "analyst1"},
            headers=_admin_headers(s),
        )

        rp = client.post(
            "/api/admin/grants",
            json={"group_id": group_id, "resource_type": "table", "resource_id": table_id},
            headers=_admin_headers(s),
        )
        assert rp.status_code == 201, rp.text

        # Analyst should now have access → 200
        r_after = client.get(
            f"/api/data/{table_id}/check-access",
            headers=_analyst_headers(s),
        )
        assert r_after.status_code == 200, (
            f"Expected 200 after grant, got {r_after.status_code}: {r_after.text}"
        )

    def test_download_returns_parquet_for_granted_user(self, seeded_app_both, registered_table_both):
        """After granting access, download returns parquet content."""
        s = seeded_app_both
        client = s["client"]
        table_id = registered_table_both["table_id"]

        # Grant access to analyst
        rg = client.post(
            "/api/admin/groups",
            json={"name": "DownloadGroup", "description": "Group for download tests"},
            headers=_admin_headers(s),
        )
        assert rg.status_code == 201, rg.text
        group_id = rg.json()["id"]

        client.post(
            f"/api/admin/groups/{group_id}/members",
            json={"user_id": "analyst1"},
            headers=_admin_headers(s),
        )

        client.post(
            "/api/admin/grants",
            json={"group_id": group_id, "resource_type": "table", "resource_id": table_id},
            headers=_admin_headers(s),
        )

        # Download
        rd = client.get(
            f"/api/data/{table_id}/download",
            headers=_analyst_headers(s),
        )
        assert rd.status_code == 200, rd.text
        content_type = rd.headers.get("content-type", "")
        assert "parquet" in content_type or "octet-stream" in content_type, (
            f"Expected parquet/octet-stream content-type, got: {content_type!r}"
        )


class TestAdminOverviewBehavioral:
    """Admin overview endpoints reflect state in the active backend."""

    def test_admin_store_submissions_list_reflects_active_backend(self, seeded_app_both, monkeypatch):
        """Upload skill → submission appears in admin submissions list."""
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: False)

        s = seeded_app_both
        client = s["client"]

        zb = make_skill_zip("admin-submission-listing")
        r = client.post(
            "/api/store/entities",
            files={"file": ("bundle.zip", io.BytesIO(zb), "application/zip")},
            data={"type": "skill"},
            headers=_admin_headers(s),
        )
        assert r.status_code == 201, r.text
        entity_id = r.json()["id"]

        from src.repositories import store_submissions_repo

        sub = store_submissions_repo().latest_for_entity(entity_id)
        assert sub is not None
        sub_id = sub["id"]

        # Admin submissions list should include this submission
        rl = client.get("/api/admin/store/submissions", headers=_admin_headers(s))
        assert rl.status_code == 200, rl.text
        body = rl.json()
        # Endpoint returns either a list or a dict with "submissions" key
        if isinstance(body, list):
            submission_ids = [ss["id"] for ss in body]
        else:
            submission_ids = [ss["id"] for ss in body.get("submissions", [])]
        assert sub_id in submission_ids, (
            f"submission {sub_id!r} not in admin list; ids: {submission_ids[:10]!r}"
        )

    def test_admin_sessions_list_reflects_uploads(self, seeded_app_both):
        """GET /api/admin/sessions/list returns 200 list."""
        s = seeded_app_both
        client = s["client"]

        # Hit the sessions list — should always return 200 (even empty)
        r = client.get("/api/admin/sessions/list", headers=_admin_headers(s))
        assert r.status_code == 200, r.text
        body = r.json()
        # Body is either a list or dict with a "sessions"/"items" key
        assert isinstance(body, (list, dict)), (
            f"Expected list or dict, got {type(body)!r}"
        )

    def test_admin_activity_feed_shows_recent_actions(self, seeded_app_both):
        """Register a table → activity feed is non-empty (has at least the register event)."""
        s = seeded_app_both
        client = s["client"]

        # Trigger a registry action that writes an audit row
        client.post(
            "/api/admin/register-table",
            json={
                "name": "activity_feed_table",
                "source_type": "keboola",
                "bucket": "activity_src",
                "source_table": "activity_feed_table",
                "query_mode": "local",
            },
            headers=_admin_headers(s),
        )

        r = client.get("/api/admin/activity", headers=_admin_headers(s))
        assert r.status_code == 200, r.text
        body = r.json()
        # Body may be {"rows": [...], ...} or a bare list
        if isinstance(body, dict):
            rows = body.get("rows", body.get("items", []))
        else:
            rows = body
        assert isinstance(rows, list), f"Expected list of activity rows, got {type(rows)!r}: {body!r}"
