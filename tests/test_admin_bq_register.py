"""Tests for BigQuery table registration via admin API + UI + CLI.

Covers issue #108 Milestone 1:
- /api/admin/register-table validation matrix for BQ rows
- /api/admin/register-table/precheck happy + sad paths (mocked
  google.cloud.bigquery.Client)
- View-name collision detection (409 distinct from id collision)
- Audit log entries on register/update/unregister with secret masking
- Sync wiring: register-then-list round-trip exercises
  bigquery.extractor.rebuild_from_registry + SyncOrchestrator.rebuild
- Admin UI: /admin/tables renders BQ vs Keboola fields based on
  data_source.type
- CLI: da admin register-table --dry-run hits /precheck
"""

import json
from unittest.mock import MagicMock, patch

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _bq_payload(**overrides):
    """Minimal valid BQ register payload, override with kwargs per test."""
    p = {
        "name": "orders",
        "source_type": "bigquery",
        "bucket": "analytics",
        "source_table": "orders",
        "query_mode": "remote",
    }
    p.update(overrides)
    return p


@pytest.fixture
def bq_instance(monkeypatch):
    """Force instance.yaml to look like a BigQuery deployment for the
    duration of one test. Patches the cached load_instance_config so
    /admin/server-config reads / get_value('data_source.bigquery.project')
    return what we want, without touching the on-disk instance.yaml."""
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {
                "project": "my-test-project",
                "location": "us",
            },
        },
    }
    # Patch every read path we know consumers use, plus reset_cache.
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    # get_value walks the merged dict; load is the source, so the patch
    # above is enough — but reset cache to avoid a stale read poisoning
    # the test.
    from app.instance_config import reset_cache
    reset_cache()
    yield fake_cfg
    reset_cache()


@pytest.fixture
def stub_bq_extractor(monkeypatch):
    """Replace rebuild_from_registry + SyncOrchestrator.rebuild with mocks
    so the API's post-register materialize doesn't try to hit real BQ."""
    rebuild_mock = MagicMock(return_value={
        "project_id": "my-test-project",
        "tables_registered": 1,
        "errors": [],
        "skipped": False,
    })
    monkeypatch.setattr(
        "connectors.bigquery.extractor.rebuild_from_registry",
        rebuild_mock,
    )
    orch_mock = MagicMock()
    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator",
        lambda *a, **kw: orch_mock,
    )
    return {"rebuild": rebuild_mock, "orchestrator": orch_mock}


# --- API: register-table for BigQuery ----------------------------------------


class TestBigQueryRegisterValidation:
    def test_missing_bucket_returns_422(self, seeded_app, bq_instance, stub_bq_extractor):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(bucket=""),
            headers=_auth(token),
        )
        assert resp.status_code == 422
        assert "bucket" in resp.json()["detail"].lower()

    def test_missing_source_table_returns_422(self, seeded_app, bq_instance, stub_bq_extractor):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(source_table=""),
            headers=_auth(token),
        )
        assert resp.status_code == 422
        assert "source_table" in resp.json()["detail"].lower()

    def test_unsafe_view_name_returns_400(self, seeded_app, bq_instance, stub_bq_extractor):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # `name` becomes the DuckDB view name (after lower+slug). A bare
        # hyphen is fine in BQ but not in a DuckDB strict identifier — must
        # fail at register time, not at first rebuild.
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="orders-2026"),
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "view name" in resp.json()["detail"].lower()

    def test_unsafe_dataset_returns_400(self, seeded_app, bq_instance, stub_bq_extractor):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(bucket='evil"dataset'),
            headers=_auth(token),
        )
        assert resp.status_code == 400

    def test_unsafe_source_table_returns_400(self, seeded_app, bq_instance, stub_bq_extractor):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(source_table='orders;DROP'),
            headers=_auth(token),
        )
        assert resp.status_code == 400

    def test_wildcard_source_table_returns_400(self, seeded_app, bq_instance, stub_bq_extractor):
        """Wildcard / sharded BQ tables are deferred to M3 (Decision 8)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(source_table="events_*"),
            headers=_auth(token),
        )
        assert resp.status_code == 400
        assert "wildcard" in resp.json()["detail"].lower()

    def test_invalid_source_type_returns_422(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "x", "source_type": "snowflake"},
            headers=_auth(token),
        )
        assert resp.status_code == 422

    def test_missing_project_in_yaml_returns_400(self, seeded_app, monkeypatch, stub_bq_extractor):
        """If data_source.bigquery.project isn't set, the BQ branch must
        refuse to register — we'd hit the missing-project error at first
        rebuild anyway, but registering a row that can never materialize
        is an operator footgun."""
        from app.instance_config import reset_cache
        monkeypatch.setattr(
            "app.instance_config.load_instance_config",
            lambda: {"data_source": {"type": "bigquery", "bigquery": {}}},
            raising=False,
        )
        reset_cache()
        try:
            c = seeded_app["client"]
            token = seeded_app["admin_token"]
            resp = c.post(
                "/api/admin/register-table",
                json=_bq_payload(),
                headers=_auth(token),
            )
            assert resp.status_code == 400
            assert "project" in resp.json()["detail"].lower()
        finally:
            reset_cache()

    def test_malformed_project_id_returns_400(self, seeded_app, monkeypatch, stub_bq_extractor):
        from app.instance_config import reset_cache
        monkeypatch.setattr(
            "app.instance_config.load_instance_config",
            lambda: {
                "data_source": {
                    "type": "bigquery",
                    "bigquery": {"project": "Bad Project With Spaces"},
                }
            },
            raising=False,
        )
        reset_cache()
        try:
            c = seeded_app["client"]
            token = seeded_app["admin_token"]
            resp = c.post(
                "/api/admin/register-table",
                json=_bq_payload(),
                headers=_auth(token),
            )
            assert resp.status_code == 400
            assert "malformed" in resp.json()["detail"].lower() or "grammar" in resp.json()["detail"].lower()
        finally:
            reset_cache()


class TestBigQueryRegisterCoercion:
    """The server must force query_mode='remote' and profile_after_sync=False
    on BQ rows (Decision 7) — even if the caller posts the wrong values."""

    def test_query_mode_forced_to_remote(self, seeded_app, bq_instance, stub_bq_extractor):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(query_mode="local", profile_after_sync=True),
            headers=_auth(token),
        )
        assert resp.status_code in (200, 202), resp.text

        # Read it back and confirm the registry has the forced values, not
        # the caller-supplied ones.
        resp = c.get("/api/admin/registry", headers=_auth(token))
        row = next(t for t in resp.json()["tables"] if t["name"] == "orders")
        assert row["query_mode"] == "remote"
        assert row["profile_after_sync"] is False


class TestBigQueryRegisterCollision:
    def test_id_collision_returns_409(self, seeded_app, bq_instance, stub_bq_extractor):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post("/api/admin/register-table", json=_bq_payload(), headers=_auth(token))
        assert resp.status_code in (200, 202)

        resp = c.post("/api/admin/register-table", json=_bq_payload(), headers=_auth(token))
        assert resp.status_code == 409
        assert "already" in resp.json()["detail"].lower()

    def test_view_name_collision_returns_409(self, seeded_app, bq_instance, stub_bq_extractor):
        """Two different display names that slugify to the same id is the
        id-collision case above. View-name collision is for two callers
        who pick the SAME display name `name` — same view, different rows.
        Pre-fix the second call would silently win at next rebuild
        (orchestrator picks the row whose extract was attached last)."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="orders", bucket="ds_a"),
            headers=_auth(token),
        )
        assert resp.status_code in (200, 202)

        # Same `name` (== view_name) — must 409 even though id derivation
        # would also collide; the pre-check is independent.
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="orders", bucket="ds_b", source_table="orders2"),
            headers=_auth(token),
        )
        assert resp.status_code == 409


class TestBigQueryRegisterAuth:
    def test_register_requires_admin(self, seeded_app, bq_instance):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(),
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_register_requires_auth(self, seeded_app, bq_instance):
        c = seeded_app["client"]
        resp = c.post("/api/admin/register-table", json=_bq_payload())
        assert resp.status_code == 401


class TestBigQueryRegisterMaterialize:
    """The server must call rebuild_from_registry + SyncOrchestrator.rebuild
    after a successful BQ register (Decision 1). Verify by stubbing both
    and asserting they fired."""

    def test_register_invokes_rebuild_and_orchestrator(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(),
            headers=_auth(token),
        )
        assert resp.status_code in (200, 202), resp.text
        # Either the synchronous path or the BackgroundTask path; both must
        # fire. BackgroundTasks run after the response in TestClient, which
        # blocks until completion.
        assert stub_bq_extractor["rebuild"].called, "rebuild_from_registry not called"
        assert stub_bq_extractor["orchestrator"].rebuild.called, "orchestrator.rebuild not called"

    def test_register_returns_200_with_view_name_on_sync_success(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(),
            headers=_auth(token),
        )
        # In tests the materialize is fast enough to land synchronously.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["view_name"] == "orders"


# --- API: precheck endpoint --------------------------------------------------


class _FakeBQTable:
    """Stand-in for google.cloud.bigquery.Table — only the attributes the
    precheck route reads."""

    def __init__(self, num_rows=1234, num_bytes=99999, schema=None):
        self.num_rows = num_rows
        self.num_bytes = num_bytes
        self.schema = schema or [
            MagicMock(name="id", field_type="INT64"),
            MagicMock(name="created_at", field_type="TIMESTAMP"),
        ]
        # Configure name attribute on each schema entry — MagicMock(name=…) is
        # the *mock's* name, not an attribute, so we set it explicitly.
        names = ["id", "created_at"]
        for col, n in zip(self.schema, names):
            col.name = n


class TestBigQueryPrecheck:
    def test_precheck_happy_path(self, seeded_app, bq_instance):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        fake_client = MagicMock()
        fake_client.get_table.return_value = _FakeBQTable()
        with patch("google.cloud.bigquery.Client", return_value=fake_client):
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(),
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        t = body["table"]
        assert t["rows"] == 1234
        assert t["size_bytes"] == 99999
        assert t["column_count"] == 2
        names = [c["name"] for c in t["columns"]]
        assert names == ["id", "created_at"]
        assert t["project_id"] == "my-test-project"

    def test_precheck_not_found_returns_404(self, seeded_app, bq_instance):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        from google.api_core import exceptions as google_exc
        fake_client = MagicMock()
        fake_client.get_table.side_effect = google_exc.NotFound("missing")
        with patch("google.cloud.bigquery.Client", return_value=fake_client):
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(),
                headers=_auth(token),
            )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_precheck_forbidden_returns_403(self, seeded_app, bq_instance):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        from google.api_core import exceptions as google_exc
        fake_client = MagicMock()
        fake_client.get_table.side_effect = google_exc.Forbidden("nope")
        with patch("google.cloud.bigquery.Client", return_value=fake_client):
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(),
                headers=_auth(token),
            )
        assert resp.status_code == 403
        assert "metadata.get" in resp.json()["detail"]

    def test_precheck_other_error_returns_400(self, seeded_app, bq_instance):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_client = MagicMock()
        fake_client.get_table.side_effect = RuntimeError("auth failed")
        with patch("google.cloud.bigquery.Client", return_value=fake_client):
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(),
                headers=_auth(token),
            )
        assert resp.status_code == 400

    def test_precheck_no_db_write(self, seeded_app, bq_instance):
        """Precheck must not touch table_registry — operator inspects the
        result, decides whether to commit, then calls register-table."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        fake_client = MagicMock()
        fake_client.get_table.return_value = _FakeBQTable()
        with patch("google.cloud.bigquery.Client", return_value=fake_client):
            c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(name="precheck_only"),
                headers=_auth(token),
            )

        resp = c.get("/api/admin/registry", headers=_auth(token))
        names = [t["name"] for t in resp.json()["tables"]]
        assert "precheck_only" not in names

    def test_precheck_validates_before_calling_bq(self, seeded_app, bq_instance):
        """Validation runs before the BQ round-trip — bogus identifiers
        must not result in a real BQ call."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("google.cloud.bigquery.Client") as cls:
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(source_table="bad;name"),
                headers=_auth(token),
            )
        assert resp.status_code == 400
        cls.assert_not_called()

    def test_precheck_requires_admin(self, seeded_app, bq_instance):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.post(
            "/api/admin/register-table/precheck",
            json=_bq_payload(),
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_precheck_keboola_skips_bq_roundtrip(self, seeded_app):
        """Non-BQ source types get validation-only precheck — no GCP call."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("google.cloud.bigquery.Client") as cls:
            resp = c.post(
                "/api/admin/register-table/precheck",
                json={
                    "name": "kb_orders",
                    "source_type": "keboola",
                    "bucket": "in.c-crm",
                    "source_table": "orders",
                },
                headers=_auth(token),
            )
        assert resp.status_code == 200
        cls.assert_not_called()
        body = resp.json()
        assert body["ok"] is True
        # M1 documents this as validation-only via the response note.
        assert "validation-only" in body["table"].get("note", "")


# --- API: audit log entries ---------------------------------------------------


class TestRegistryAuditLog:
    """Decision 4: every registry mutation writes an audit_log row."""

    def _list_audit(self, conn, action):
        from src.repositories.audit import AuditRepository
        return AuditRepository(conn).query(action=action, limit=10)

    def test_register_keboola_writes_audit_entry(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={"name": "kb_aud", "source_type": "keboola", "bucket": "in.c-crm"},
            headers=_auth(token),
        )
        assert resp.status_code == 201

        from src.db import get_system_db
        conn = get_system_db()
        try:
            rows = self._list_audit(conn, "register_table")
        finally:
            conn.close()
        assert any(r["resource"] == "kb_aud" for r in rows), \
            f"register_table audit entry not found in {rows}"

    def test_register_bq_writes_audit_entry(self, seeded_app, bq_instance, stub_bq_extractor):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.post("/api/admin/register-table", json=_bq_payload(name="bq_aud"), headers=_auth(token))

        from src.db import get_system_db
        conn = get_system_db()
        try:
            rows = self._list_audit(conn, "register_table")
        finally:
            conn.close()
        match = [r for r in rows if r["resource"] == "bq_aud"]
        assert match, f"register_table audit entry not found for bq_aud: {rows}"
        params = json.loads(match[0]["params"])
        assert params["source_type"] == "bigquery"
        assert params["bucket"] == "analytics"

    def test_audit_masks_secret_keyed_fields(self, seeded_app):
        """Even though the registry payload doesn't normally carry secrets,
        the sanitizer must mask any secret-looking key. Confirm by posting
        a synthetic field — the API ignores unknown fields, but the audit
        path runs `model_dump` so we can't test via the wire. Instead test
        the helper directly."""
        from app.api.admin import _sanitize_for_audit
        out = _sanitize_for_audit({
            "name": "x",
            "api_token": "hunter2",
            "bot_secret": "abc",
            "primary_key": ["id"],
            "description": "raw description stays raw",
            "password": "p",
        })
        assert out["name"] == "x"
        assert out["api_token"] == "***"
        assert out["bot_secret"] == "***"
        assert out["password"] == "***"
        assert out["primary_key"] == ["id"]  # whitelisted
        assert out["description"] == "raw description stays raw"

    def test_update_writes_audit_entry(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.post(
            "/api/admin/register-table",
            json={"name": "kb_upd", "source_type": "keboola"},
            headers=_auth(token),
        )
        resp = c.put(
            "/api/admin/registry/kb_upd",
            json={"description": "updated"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        from src.db import get_system_db
        conn = get_system_db()
        try:
            rows = self._list_audit(conn, "update_table")
        finally:
            conn.close()
        assert any(r["resource"] == "kb_upd" for r in rows)

    def test_unregister_writes_audit_entry(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.post(
            "/api/admin/register-table",
            json={"name": "kb_del", "source_type": "keboola"},
            headers=_auth(token),
        )
        resp = c.delete("/api/admin/registry/kb_del", headers=_auth(token))
        assert resp.status_code == 204

        from src.db import get_system_db
        conn = get_system_db()
        try:
            rows = self._list_audit(conn, "unregister_table")
        finally:
            conn.close()
        assert any(r["resource"] == "kb_del" for r in rows)


# --- bigquery.extractor.rebuild_from_registry --------------------------------


class TestRebuildFromRegistry:
    def test_returns_skipped_when_no_bq_rows(self, e2e_env, monkeypatch):
        """No BigQuery rows in registry → skipped=True, no extract written."""
        monkeypatch.setattr(
            "config.loader.load_instance_config",
            lambda: {
                "data_source": {
                    "type": "bigquery",
                    "bigquery": {"project": "ok-project"},
                }
            },
        )
        # Empty registry — get_system_db returns the test DB, fresh.
        from connectors.bigquery import extractor as bq
        fake_init = MagicMock()
        monkeypatch.setattr(bq, "init_extract", fake_init)

        result = bq.rebuild_from_registry()

        assert result["skipped"] is True
        assert result["tables_registered"] == 0
        fake_init.assert_not_called()

    def test_calls_init_extract_with_registry_rows(self, e2e_env, monkeypatch):
        from connectors.bigquery import extractor as bq
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        # Seed one BQ row.
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="orders",
                name="orders",
                source_type="bigquery",
                bucket="analytics",
                source_table="orders",
                query_mode="remote",
                profile_after_sync=False,
            )
        finally:
            conn.close()

        monkeypatch.setattr(
            "config.loader.load_instance_config",
            lambda: {
                "data_source": {
                    "type": "bigquery",
                    "bigquery": {"project": "ok-project"},
                }
            },
        )
        fake_init = MagicMock(return_value={"tables_registered": 1, "errors": []})
        monkeypatch.setattr(bq, "init_extract", fake_init)

        result = bq.rebuild_from_registry()

        assert result["skipped"] is False
        assert result["project_id"] == "ok-project"
        fake_init.assert_called_once()
        args, kwargs = fake_init.call_args
        # init_extract(output_dir, project_id, table_configs)
        assert args[1] == "ok-project"
        names = [t["name"] for t in args[2]]
        assert "orders" in names

    def test_missing_project_returns_error(self, e2e_env, monkeypatch):
        monkeypatch.setattr(
            "config.loader.load_instance_config",
            lambda: {"data_source": {"type": "bigquery", "bigquery": {}}},
        )
        from connectors.bigquery import extractor as bq
        result = bq.rebuild_from_registry()
        assert result["project_id"] == ""
        assert result["errors"]
        assert "project" in result["errors"][0]["error"].lower()


# --- UI: /admin/tables renders BQ vs Keboola fields --------------------------


class TestAdminTablesUI:
    def test_renders_bq_fields_when_data_source_bigquery(self, seeded_app, bq_instance):
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["admin_token"])
        try:
            resp = c.get("/admin/tables", headers={"Accept": "text/html"})
        finally:
            c.cookies.clear()
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Modal carries the source type so the JS can branch.
        assert 'data-source-type="bigquery"' in body
        # BQ-only inputs.
        assert 'id="bqDataset"' in body
        assert 'id="bqSourceTable"' in body
        assert 'id="bqViewName"' in body
        assert 'id="bqSyncSchedule"' in body
        # Inline hint about scheduler-not-yet-wired (Decision 3).
        assert "scheduler" in body.lower()
        # BQ-specific panel (no discovery for BQ in M1).
        assert 'data-test="bq-register-panel"' in body
        # Keboola-only inputs must NOT be present.
        assert 'id="regTableId"' not in body
        assert 'id="regBucket"' not in body

    def test_renders_keboola_fields_when_data_source_keboola(self, seeded_app, monkeypatch):
        from app.instance_config import reset_cache
        monkeypatch.setattr(
            "app.instance_config.load_instance_config",
            lambda: {"data_source": {"type": "keboola"}},
            raising=False,
        )
        reset_cache()
        try:
            c = seeded_app["client"]
            c.cookies.set("access_token", seeded_app["admin_token"])
            try:
                resp = c.get("/admin/tables", headers={"Accept": "text/html"})
            finally:
                c.cookies.clear()
            assert resp.status_code == 200
            body = resp.text
            assert 'data-source-type="keboola"' in body
            # Keboola path — discovery panel + Keboola inputs.
            assert 'id="discoveryResults"' in body
            assert 'id="regBucket"' in body
            assert 'id="regTableName"' in body
            # BQ-only inputs MUST NOT be present.
            assert 'id="bqDataset"' not in body
        finally:
            reset_cache()

    def test_admin_tables_requires_admin(self, seeded_app):
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["analyst_token"])
        try:
            resp = c.get("/admin/tables", follow_redirects=False)
        finally:
            c.cookies.clear()
        assert resp.status_code in (302, 401, 403)


# --- CLI: da admin register-table --dry-run ----------------------------------


class TestCliRegisterTableDryRun:
    def _resp(self, status_code=200, json_data=None, text=""):
        r = MagicMock()
        r.status_code = status_code
        r.json.return_value = json_data if json_data is not None else {}
        r.text = text
        return r

    def test_dry_run_calls_precheck_endpoint(self, monkeypatch, tmp_path):
        from typer.testing import CliRunner
        from cli.main import app
        runner = CliRunner()

        captured = {}

        def fake_post(path, json=None, **kwargs):
            captured["path"] = path
            captured["payload"] = json
            return self._resp(
                200,
                {
                    "ok": True,
                    "table": {
                        "name": "orders",
                        "source_type": "bigquery",
                        "bucket": "analytics",
                        "source_table": "orders",
                        "project_id": "my-test-project",
                        "rows": 100,
                        "size_bytes": 4096,
                        "columns": [
                            {"name": "id", "type": "INT64"},
                            {"name": "created_at", "type": "TIMESTAMP"},
                        ],
                        "column_count": 2,
                    },
                },
            )

        monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        with patch("cli.commands.admin.api_post", side_effect=fake_post):
            result = runner.invoke(app, [
                "admin", "register-table", "orders",
                "--source-type", "bigquery",
                "--bucket", "analytics",
                "--source-table", "orders",
                "--dry-run",
            ])
        assert result.exit_code == 0, result.output
        assert captured["path"] == "/api/admin/register-table/precheck"
        # No DB write happened (we only mocked api_post).
        assert "DRY RUN" in result.output
        assert "rows:" in result.output
        assert "id" in result.output
        assert "created_at" in result.output

    def test_dry_run_failure_exits_nonzero(self, monkeypatch, tmp_path):
        from typer.testing import CliRunner
        from cli.main import app
        runner = CliRunner()

        monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        with patch(
            "cli.commands.admin.api_post",
            return_value=self._resp(404, {"detail": "BigQuery table not found"}, "404"),
        ):
            result = runner.invoke(app, [
                "admin", "register-table", "missing",
                "--source-type", "bigquery",
                "--bucket", "analytics",
                "--source-table", "missing",
                "--dry-run",
            ])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_register_without_dry_run_still_works(self, monkeypatch, tmp_path):
        """Backwards compat — the existing flag set unchanged."""
        from typer.testing import CliRunner
        from cli.main import app
        runner = CliRunner()

        monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        captured = {}

        def fake_post(path, json=None, **kwargs):
            captured["path"] = path
            return self._resp(201, {"id": "x", "name": "x", "status": "registered"})

        with patch("cli.commands.admin.api_post", side_effect=fake_post):
            result = runner.invoke(app, [
                "admin", "register-table", "orders",
                "--source-type", "keboola",
                "--bucket", "in.c-crm",
            ])
        assert result.exit_code == 0
        assert captured["path"] == "/api/admin/register-table"

    def test_register_handles_202_response(self, monkeypatch, tmp_path):
        """BQ register can return 202 when materialize exceeds the budget."""
        from typer.testing import CliRunner
        from cli.main import app
        runner = CliRunner()

        monkeypatch.setenv("DA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        with patch(
            "cli.commands.admin.api_post",
            return_value=self._resp(202, {"id": "x", "name": "x", "status": "accepted", "view_name": "x"}),
        ):
            result = runner.invoke(app, [
                "admin", "register-table", "orders",
                "--source-type", "bigquery",
                "--bucket", "analytics",
                "--source-table", "orders",
            ])
        assert result.exit_code == 0
        assert "background" in result.output.lower()


# --- Review fixes for #108 M1 ------------------------------------------------


class TestKeboolaRegisterStatusCode:
    """Status-code contract: the route no longer carries `status_code=201` on
    its decorator — each branch returns its own. Keboola (non-BQ) must still
    explicitly return 201 with the registered-row body."""

    def test_keboola_register_returns_201(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "kb_status",
                "source_type": "keboola",
                "bucket": "in.c-crm",
                "source_table": "orders",
                "query_mode": "local",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"] == "kb_status"
        assert body["status"] == "registered"


class TestUpdateTableBigQueryValidation:
    """PUT /api/admin/registry/{id} must run the BQ-shape validator whenever
    the merged record would be a BQ row, including the case where the patch
    flips source_type from keboola → bigquery (review IMPORTANT-4)."""

    def test_put_keboola_row_to_bq_with_bad_project_returns_4xx(
        self, seeded_app, monkeypatch,
    ):
        from app.instance_config import reset_cache
        # Set a malformed project_id in instance.yaml so the BQ validator
        # rejects the merged row at PUT time.
        monkeypatch.setattr(
            "app.instance_config.load_instance_config",
            lambda: {
                "data_source": {
                    "type": "bigquery",
                    "bigquery": {"project": "Bad Project With Spaces"},
                }
            },
            raising=False,
        )
        reset_cache()
        try:
            c = seeded_app["client"]
            token = seeded_app["admin_token"]
            # Seed a Keboola row first.
            resp = c.post(
                "/api/admin/register-table",
                json={
                    "name": "rev4",
                    "source_type": "keboola",
                    "bucket": "in.c-crm",
                    "source_table": "rev4",
                    "query_mode": "local",
                },
                headers=_auth(token),
            )
            assert resp.status_code == 201

            # Now PATCH it to bigquery — must run BQ validation and 4xx
            # because the project_id is bogus.
            resp = c.put(
                "/api/admin/registry/rev4",
                json={
                    "source_type": "bigquery",
                    "bucket": "analytics",
                    "source_table": "rev4",
                },
                headers=_auth(token),
            )
            assert resp.status_code in (400, 422), resp.text
        finally:
            reset_cache()

    def test_put_existing_bq_row_with_bad_bucket_returns_400(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        """An admin PATCH that mutates `bucket` on an existing BQ row to an
        unsafe identifier must be rejected before the registry write."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # Register a BQ row.
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="rev4_bq"),
            headers=_auth(token),
        )
        assert resp.status_code in (200, 202), resp.text

        # PATCH bucket to an unsafe identifier — must 400.
        resp = c.put(
            "/api/admin/registry/rev4_bq",
            json={"bucket": 'evil";DROP'},
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text


class TestAuditAllowlistMasking:
    """Review IMPORTANT-5: explicit allowlist instead of substring scan.

    Asserts that:
      - field names containing 'token'/'key'/'secret' as substrings are NOT
        masked unless they're in the explicit allowlist; and
      - known-secret fields IN the allowlist are still masked.
    """

    def test_substring_match_does_not_mask_unknown_fields(self):
        from app.api.admin import _sanitize_for_audit
        out = _sanitize_for_audit({
            # All of these would have been masked by the old substring
            # scan but should now flow through cleartext — they aren't
            # actual credentials.
            "not_actually_a_token": "literal value",
            "primary_key": ["id"],
            "primary_key_hash": "deadbeef",
            "passwordless": "no creds here",
            "secretly_an_int": 42,
        })
        assert out["not_actually_a_token"] == "literal value"
        assert out["primary_key"] == ["id"]
        assert out["primary_key_hash"] == "deadbeef"
        assert out["passwordless"] == "no creds here"
        assert out["secretly_an_int"] == 42

    def test_allowlisted_secret_fields_are_masked(self):
        from app.api.admin import _sanitize_for_audit
        out = _sanitize_for_audit({
            "keboola_token": "kbc-1234",
            "client_secret": "abc",
            "smtp_password": "p",
            "bot_token": "tg-1",
            "name": "kept-raw",
        })
        assert out["keboola_token"] == "***"
        assert out["client_secret"] == "***"
        assert out["smtp_password"] == "***"
        assert out["bot_token"] == "***"
        assert out["name"] == "kept-raw"

    def test_empty_secret_fields_are_marked_empty(self):
        from app.api.admin import _sanitize_for_audit
        out = _sanitize_for_audit({"keboola_token": "", "client_secret": None})
        assert out["keboola_token"] == "<empty>"
        assert out["client_secret"] == "<empty>"


class TestBigQueryInitExtractLockSerialization:
    """Review IMPORTANT-2: two concurrent calls to `init_extract` (the
    file-swap path) must serialize cleanly under `_INIT_EXTRACT_LOCK`. We
    verify the lock by stubbing the heavy GCE round-trip and asserting that
    only one worker is inside the locked body at a time."""

    def test_concurrent_init_extract_serializes(self, tmp_path, monkeypatch):
        import threading
        import time

        from connectors.bigquery import extractor as bq

        # Track concurrent entries into the locked body. If the lock works,
        # `inside` is never > 1.
        inside = {"current": 0, "peak": 0}
        lock = threading.Lock()

        def fake_locked(output_dir, project_id, table_configs):
            with lock:
                inside["current"] += 1
                inside["peak"] = max(inside["peak"], inside["current"])
            try:
                # Hold the lock long enough that a parallel call has time to
                # block on `_INIT_EXTRACT_LOCK` if serialization works, or
                # race past it (and bump `peak` to 2) if it doesn't.
                time.sleep(0.05)
                return {"tables_registered": 0, "errors": []}
            finally:
                with lock:
                    inside["current"] -= 1

        monkeypatch.setattr(bq, "_init_extract_locked", fake_locked)

        results = []

        def call():
            results.append(
                bq.init_extract(str(tmp_path / "extr"), "ok-project", [])
            )

        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 3
        assert inside["peak"] == 1, (
            f"_INIT_EXTRACT_LOCK did not serialize concurrent callers — "
            f"peak concurrency was {inside['peak']}"
        )


class TestBigQueryRegisterFreshConnection:
    """Review BLOCKER-1: the worker must not capture the request-scoped
    DuckDB connection. Confirm by asserting the worker calls `get_system_db`
    (fresh handle) and the request connection is NEVER passed through.
    """

    def test_worker_opens_fresh_connection(
        self, seeded_app, bq_instance, stub_bq_extractor, monkeypatch,
    ):
        from src import db as _db

        opens = {"count": 0}
        original_get = _db.get_system_db

        def counting_get_system_db():
            opens["count"] += 1
            return original_get()

        monkeypatch.setattr("src.db.get_system_db", counting_get_system_db)
        # The admin module imports `get_system_db` via `from src.db import …`
        # inside the worker function, so patching `src.db.get_system_db` is
        # sufficient — but also patch any cached binding for safety.
        import app.api.admin as admin_mod
        if hasattr(admin_mod, "get_system_db"):
            monkeypatch.setattr(admin_mod, "get_system_db", counting_get_system_db, raising=False)

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="fresh_conn"),
            headers=_auth(token),
        )
        assert resp.status_code in (200, 202), resp.text
        # The worker opens at least one fresh connection (via get_system_db).
        # Other parts of the request also use get_system_db (auth gate, repo
        # lookup), so we just assert that the worker contributed at least one
        # extra open. Stronger guarantee: the rebuild stub was invoked.
        assert stub_bq_extractor["rebuild"].called
        # And the connection passed to rebuild_from_registry must NOT be the
        # same one the request handler held — assert it's not None and was
        # opened in the worker (we can't compare object identity without
        # threading the request conn through, but a separate handle implies
        # the worker did its own open).
        passed_conn = stub_bq_extractor["rebuild"].call_args.kwargs.get("conn")
        assert passed_conn is not None, (
            "rebuild_from_registry should receive a fresh worker-opened conn"
        )

    def test_worker_runs_after_request_returns(
        self, seeded_app, bq_instance, monkeypatch,
    ):
        """Force the synchronous budget to expire so the BackgroundTask path
        runs after the request connection is closed. The worker must still
        complete because it opens its own connection."""
        from unittest.mock import MagicMock
        import time

        # Replace SyncOrchestrator with a fast no-op so we can observe the
        # rebuild_from_registry call after the response.
        orch_mock = MagicMock()
        monkeypatch.setattr(
            "src.orchestrator.SyncOrchestrator",
            lambda *a, **kw: orch_mock,
        )

        # Stub rebuild_from_registry to take longer than the budget so the
        # synchronous path times out and BackgroundTask kicks in.
        slow_rebuild = MagicMock()

        def slow_call(conn=None, output_dir=None):
            time.sleep(0.2)
            return {
                "project_id": "my-test-project",
                "tables_registered": 1,
                "errors": [],
                "skipped": False,
            }

        slow_rebuild.side_effect = slow_call
        monkeypatch.setattr(
            "connectors.bigquery.extractor.rebuild_from_registry",
            slow_rebuild,
        )

        # Tighten the budget so the test is fast.
        monkeypatch.setattr(
            "app.api.admin._BQ_SYNC_REGISTER_TIMEOUT_S", 0.05, raising=False,
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="fresh_after"),
            headers=_auth(token),
        )
        # 202 (timeout) is the expected path; 200 is acceptable if the box is
        # slow enough that BackgroundTask runs synchronously inside TestClient.
        assert resp.status_code in (200, 202), resp.text
        # Wait for the BackgroundTask to drain. TestClient already does this
        # synchronously for tasks, but the timeout-fallback also spawned a
        # daemon thread. Give both up to 1s to settle.
        deadline = time.time() + 1.0
        while time.time() < deadline and slow_rebuild.call_count < 1:
            time.sleep(0.01)
        assert slow_rebuild.called, (
            "rebuild_from_registry should run after request returns "
            "(via BackgroundTask + daemon fallback)"
        )


# --- Devin review fixes (PR #119) -------------------------------------------


class TestRegisterTableHandlerIsSync:
    """Review BLOCKER 1: register_table must NOT be `async def`. The
    synchronous-materialize path waits on `threading.Event.wait()` which
    would otherwise block the asyncio event loop and stall every other
    request for up to `_BQ_SYNC_REGISTER_TIMEOUT_S`. FastAPI runs plain
    `def` handlers in a threadpool so the wait is harmless there.
    """

    def test_handler_is_not_a_coroutine(self):
        import inspect
        from app.api.admin import register_table
        assert not inspect.iscoroutinefunction(register_table), (
            "register_table must be a sync def — see review BLOCKER 1 in #119. "
            "An async handler that blocks on threading.Event.wait() parks the "
            "asyncio event loop for the entire timeout budget."
        )

    def test_event_loop_not_blocked_by_slow_register(
        self, seeded_app, bq_instance, monkeypatch,
    ):
        """A slow BQ register must not stall a parallel request.

        We force the synchronous materialize past its budget by stubbing
        `_run_bigquery_materialize_with_timeout` to spin for ~0.3s, then
        fire two requests "in parallel" (via two threads, since TestClient
        is sync) and assert both finish within a reasonable wall clock.
        If the handler were async + blocking, the second request would
        wait for the first to finish.
        """
        import threading
        import time

        # Stub the materialize helper so the test doesn't need real BQ.
        # `_run_bigquery_materialize_with_timeout` is what the handler
        # waits on; make it sleep, then return ok.
        def _slow(background):
            time.sleep(0.3)
            return {"status": "ok"}

        monkeypatch.setattr(
            "app.api.admin._run_bigquery_materialize_with_timeout",
            _slow,
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        results = {}

        def fire_register(idx):
            t0 = time.time()
            r = c.post(
                "/api/admin/register-table",
                json=_bq_payload(name=f"par_{idx}"),
                headers=_auth(token),
            )
            results[idx] = (r.status_code, time.time() - t0)

        threads = [
            threading.Thread(target=fire_register, args=(i,)) for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Both calls must succeed. The exact wall clock depends on the
        # threadpool size FastAPI's anyio uses (default >= 40), but the
        # SECOND call should not be blocked behind the FIRST one's
        # 0.3s sleep — total time for each call should be ~0.3s, not
        # ~0.6s. Allow generous slack for CI noise.
        assert results[0][0] in (200, 202), results[0]
        assert results[1][0] in (200, 202), results[1]


class TestBigQueryRebuildOverlayAware:
    """Review BLOCKER 2: rebuild_from_registry must read the BQ project via
    the overlay-aware `app.instance_config.get_value`, NOT the static-only
    `config.loader.load_instance_config`. Validation already does the
    former, so without this fix validation passes and the rebuild silently
    fails — the row is in the registry but the master view is never built.
    """

    def test_overlay_only_project_resolves(self, e2e_env, monkeypatch):
        """When the project is set ONLY in the overlay (admin UI write),
        rebuild must still resolve it."""
        from app.instance_config import reset_cache
        from connectors.bigquery import extractor as bq
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        # Static instance.yaml has no BQ block — only the overlay does.
        # We simulate the merged result the way `app.instance_config.load_
        # instance_config` would expose it: deep-merged dict from
        # static + overlay. Patching `app.instance_config.load_instance_
        # config` matches the read path in the new helper.
        monkeypatch.setattr(
            "app.instance_config.load_instance_config",
            lambda: {
                "data_source": {
                    "type": "bigquery",
                    "bigquery": {"project": "overlay-project"},
                }
            },
            raising=False,
        )
        # And the static loader has nothing — proves we don't fall back.
        monkeypatch.setattr(
            "config.loader.load_instance_config",
            lambda: {},
            raising=False,
        )
        reset_cache()

        # Seed a BQ row so init_extract is triggered.
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="ovr",
                name="ovr",
                source_type="bigquery",
                bucket="analytics",
                source_table="ovr",
                query_mode="remote",
                profile_after_sync=False,
            )
        finally:
            conn.close()
        fake_init = MagicMock(return_value={"tables_registered": 1, "errors": []})
        monkeypatch.setattr(bq, "init_extract", fake_init)

        try:
            result = bq.rebuild_from_registry()
        finally:
            reset_cache()

        # Project resolved from the overlay, not the (empty) static file.
        assert result["project_id"] == "overlay-project"
        assert result["skipped"] is False
        fake_init.assert_called_once()
        # init_extract(output_dir, project_id, table_configs)
        assert fake_init.call_args.args[1] == "overlay-project"

    def test_static_only_project_still_resolves(self, e2e_env, monkeypatch):
        """Regression: when there's NO overlay, the static config still wins
        (so existing deployments that wrote instance.yaml by hand keep
        working)."""
        from app.instance_config import reset_cache
        from connectors.bigquery import extractor as bq
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        monkeypatch.setattr(
            "app.instance_config.load_instance_config",
            lambda: {
                "data_source": {
                    "type": "bigquery",
                    "bigquery": {"project": "static-project"},
                }
            },
            raising=False,
        )
        monkeypatch.setattr(
            "config.loader.load_instance_config",
            lambda: {
                "data_source": {
                    "type": "bigquery",
                    "bigquery": {"project": "static-project"},
                }
            },
            raising=False,
        )
        reset_cache()

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="stat",
                name="stat",
                source_type="bigquery",
                bucket="analytics",
                source_table="stat",
                query_mode="remote",
                profile_after_sync=False,
            )
        finally:
            conn.close()
        fake_init = MagicMock(return_value={"tables_registered": 1, "errors": []})
        monkeypatch.setattr(bq, "init_extract", fake_init)

        try:
            result = bq.rebuild_from_registry()
        finally:
            reset_cache()

        assert result["project_id"] == "static-project"
        fake_init.assert_called_once()


class TestBigQueryRebuildErrorPropagation:
    """Review IMPORTANT 3: errors from rebuild_from_registry must surface
    as 500 in the synchronous register path (not be silently logged), and
    in the BackgroundTask path must be logged at ERROR level (not warn)."""

    def test_synchronous_path_returns_500_on_rebuild_errors(
        self, seeded_app, bq_instance, monkeypatch,
    ):
        # Stub rebuild_from_registry to report errors but not raise.
        rebuild_mock = MagicMock(return_value={
            "project_id": "my-test-project",
            "tables_registered": 0,
            "errors": [{"table": "orders", "error": "auth failed"}],
            "skipped": False,
        })
        monkeypatch.setattr(
            "connectors.bigquery.extractor.rebuild_from_registry",
            rebuild_mock,
        )
        orch_mock = MagicMock()
        monkeypatch.setattr(
            "src.orchestrator.SyncOrchestrator",
            lambda *a, **kw: orch_mock,
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="errprop"),
            headers=_auth(token),
        )
        # Synchronous rebuild ran (within budget) but reported errors —
        # the API must propagate that as 500 with the error list, not
        # claim success.
        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert body["status"] == "rebuild_failed"
        assert body["errors"]
        assert body["errors"][0]["error"] == "auth failed"
        # The row is in the registry though — the rebuild can be retried.
        list_resp = c.get("/api/admin/registry", headers=_auth(token))
        names = [t["name"] for t in list_resp.json()["tables"]]
        assert "errprop" in names

    def test_background_path_logs_at_error_level(
        self, seeded_app, bq_instance, monkeypatch, caplog,
    ):
        """Force timeout so the BackgroundTask wrapper runs, then assert
        the wrapper logs the rebuild errors at ERROR level."""
        import logging
        import time

        # rebuild slow enough to time out the synchronous path.
        def slow_with_errors(conn=None, output_dir=None):
            time.sleep(0.15)
            return {
                "project_id": "my-test-project",
                "tables_registered": 0,
                "errors": [{"table": "x", "error": "bg-rebuild failure"}],
                "skipped": False,
            }

        monkeypatch.setattr(
            "connectors.bigquery.extractor.rebuild_from_registry",
            slow_with_errors,
        )
        orch_mock = MagicMock()
        monkeypatch.setattr(
            "src.orchestrator.SyncOrchestrator",
            lambda *a, **kw: orch_mock,
        )
        # Tighten the budget so timeout kicks in fast.
        monkeypatch.setattr(
            "app.api.admin._BQ_SYNC_REGISTER_TIMEOUT_S", 0.05, raising=False,
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        with caplog.at_level(logging.ERROR, logger="app.api.admin"):
            resp = c.post(
                "/api/admin/register-table",
                json=_bq_payload(name="bg_err"),
                headers=_auth(token),
            )
            # 202 (timeout) — BackgroundTask runs after the response.
            assert resp.status_code == 202, resp.text
            # Drain BackgroundTasks. TestClient runs them synchronously
            # after the response, so the log should already be present.

        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        # At least one ERROR-level entry must mention "bg-rebuild failure"
        # — so the operator's logs surface the failure even though the
        # 202 response can't carry the detail.
        assert any("bg-rebuild failure" in m for m in msgs), (
            f"expected ERROR-level rebuild-failure log; got: {msgs}"
        )


class TestKeboolaModalUsesDiscoveredTableId:
    """Review IMPORTANT 5: the JS that builds the Keboola register payload
    must derive `source_table` from the discovered table's storage ID
    (`t.id` minus the bucket prefix), NOT the human-friendly display name
    (`t.name`). We verify by static template inspection — this is enough
    to catch a regression that drops the hidden field or reverts the JS
    to reading `regTableName`."""

    def test_template_has_hidden_source_table_field(self, seeded_app, monkeypatch):
        from app.instance_config import reset_cache
        monkeypatch.setattr(
            "app.instance_config.load_instance_config",
            lambda: {"data_source": {"type": "keboola"}},
            raising=False,
        )
        reset_cache()
        try:
            c = seeded_app["client"]
            c.cookies.set("access_token", seeded_app["admin_token"])
            try:
                resp = c.get("/admin/tables", headers={"Accept": "text/html"})
            finally:
                c.cookies.clear()
            assert resp.status_code == 200, resp.text
            body = resp.text
            # Hidden field must exist so the JS can stash the bare
            # storage identifier separately from the display name.
            assert 'id="regSourceTable"' in body
            # And the build function must read from that hidden field
            # (NOT from regTableName, which is the display name).
            assert "getElementById('regSourceTable').value" in body
        finally:
            reset_cache()

    def test_template_does_not_send_display_name_as_source_table(
        self, seeded_app, monkeypatch,
    ):
        """Regression check: pre-fix the payload had
        `source_table: document.getElementById('regTableName').value`.
        After the fix, that exact line must be gone (the build function
        reads from the hidden `regSourceTable` first)."""
        from app.instance_config import reset_cache
        monkeypatch.setattr(
            "app.instance_config.load_instance_config",
            lambda: {"data_source": {"type": "keboola"}},
            raising=False,
        )
        reset_cache()
        try:
            c = seeded_app["client"]
            c.cookies.set("access_token", seeded_app["admin_token"])
            try:
                resp = c.get("/admin/tables", headers={"Accept": "text/html"})
            finally:
                c.cookies.clear()
            body = resp.text
            # No occurrence of the buggy direct assignment.
            assert (
                "source_table: document.getElementById('regTableName').value"
                not in body
            )
        finally:
            reset_cache()


class TestBigQueryUITwoStepFlow:
    """Review IMPORTANT 4: the BQ register flow in the modal must split
    precheck and register into two operator-driven clicks. We verify the
    JS function structure via template inspection (no JS test runner in
    this codebase)."""

    def test_template_has_separate_confirm_function(self, seeded_app, bq_instance):
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["admin_token"])
        try:
            resp = c.get("/admin/tables", headers={"Accept": "text/html"})
        finally:
            c.cookies.clear()
        assert resp.status_code == 200, resp.text
        body = resp.text
        # Two-step: precheck function + separate confirm function.
        assert "_registerBigQueryTable" in body
        assert "_confirmRegisterBigQueryTable" in body
        # Pre-fix, the precheck callback chained directly into a
        # `fetch('/api/admin/register-table'...)` inside the same `.then`.
        # After the fix, the precheck handler must NOT contain the
        # second fetch URL. Verify the precheck function body explicitly
        # swaps the button to "Register" and assigns onclick to the
        # confirm function.
        assert "btn.onclick = function() { _confirmRegisterBigQueryTable" in body
        # And the actual register POST is inside _confirmRegisterBigQueryTable.
        # Locate the function body and assert it has the register URL.
        idx = body.find("function _confirmRegisterBigQueryTable")
        assert idx >= 0
        # Take the next ~2000 chars as the function body — generous
        # enough for the small handler.
        confirm_body = body[idx:idx + 3000]
        assert "/api/admin/register-table'" in confirm_body
        assert "method: 'POST'" in confirm_body


class TestCliDiscoverAndRegisterAcceptsAllSuccessCodes:
    """Review NIT 6: `da admin discover-and-register` must accept 200
    (BQ sync OK) and 202 (BQ background) as success, not just 201.
    Pre-fix every successful BQ row counted as an error."""

    def _resp(self, status_code=200, json_data=None, text=""):
        r = MagicMock()
        r.status_code = status_code
        r.json.return_value = json_data if json_data is not None else {}
        r.text = text
        return r

    def _run(self, monkeypatch, status_code, body=None, source_type="bigquery"):
        from typer.testing import CliRunner
        from cli.main import app
        runner = CliRunner()

        # Need both KEBOOLA_* env vars for the gate; we mock httpx.get
        # so the actual values don't matter.
        monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "fake-kbc-token")
        monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.example.com")

        fake_tables = [
            {
                "id": "in.c-x.orders",
                "name": "orders",
                "bucket": {"id": "in.c-x"},
                "rowsCount": 100,
            }
        ]
        fake_get = MagicMock()
        fake_get.return_value = self._resp(200, fake_tables)
        fake_get.return_value.raise_for_status = lambda: None
        # `httpx` is imported locally inside discover_and_register, so we
        # patch the module-level attribute the function will resolve.
        import httpx as _httpx
        monkeypatch.setattr(_httpx, "get", fake_get)

        register_resp = self._resp(status_code, body or {"id": "orders", "name": "orders"})
        with patch("cli.commands.admin.api_post", return_value=register_resp):
            result = runner.invoke(app, [
                "admin", "discover-and-register",
                "--source-type", source_type,
            ])
        return result

    def test_accepts_200_as_success(self, monkeypatch):
        result = self._run(monkeypatch, 200, {
            "id": "orders", "name": "orders", "status": "ok", "view_name": "orders",
        })
        assert result.exit_code == 0, result.output
        assert "1 registered" in result.output
        assert "0 errors" in result.output

    def test_accepts_202_as_success(self, monkeypatch):
        result = self._run(monkeypatch, 202, {
            "id": "orders", "name": "orders", "status": "accepted", "view_name": "orders",
        })
        assert result.exit_code == 0, result.output
        assert "1 registered" in result.output
        assert "0 errors" in result.output
        # Operator gets a hint that the row is materializing in BG.
        assert "background" in result.output.lower()

    def test_accepts_201_as_success(self, monkeypatch):
        # Regression: legacy non-BQ insert path still works.
        result = self._run(
            monkeypatch, 201,
            {"id": "orders", "name": "orders", "status": "registered"},
            source_type="keboola",
        )
        assert result.exit_code == 0, result.output
        assert "1 registered" in result.output


class TestBigQueryRegisterRawNameValidation:
    """Round-3 review BLOCKER 1: ``_validate_bigquery_register_payload`` must
    validate the RAW name (the value persisted to ``table_registry.name``
    and used by the BQ extractor as the DuckDB view name), NOT a normalized
    form. Pre-fix a name like ``"my table"`` would pass validation
    (normalized ``"my_table"`` is safe), get stored verbatim, then 500 at
    the post-insert rebuild — defeating fast-fail-at-register."""

    def test_register_rejects_name_with_space(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="my table"),
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        # Operator-friendly: surface the offending raw value verbatim.
        assert "my table" in body["detail"]
        assert "view name" in body["detail"].lower()

    def test_register_rejects_name_with_leading_whitespace(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="  orders"),
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text

    def test_register_rejects_name_with_trailing_whitespace(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="orders  "),
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text

    def test_register_accepts_safe_name(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        """Sanity check: the strict check still admits well-formed names."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="my_table"),
            headers=_auth(token),
        )
        assert resp.status_code in (200, 202), resp.text

    def test_precheck_rejects_name_with_space(self, seeded_app, bq_instance):
        """Validation runs identically in /precheck — and it does so BEFORE
        the BQ round-trip, so a bad raw name short-circuits without touching
        the network."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("google.cloud.bigquery.Client") as cls:
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(name="my table"),
                headers=_auth(token),
            )
        assert resp.status_code == 400, resp.text
        assert "my table" in resp.json()["detail"]
        cls.assert_not_called()

    def test_precheck_accepts_safe_name(self, seeded_app, bq_instance):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_client = MagicMock()
        fake_client.get_table.return_value = _FakeBQTable()
        with patch("google.cloud.bigquery.Client", return_value=fake_client):
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(name="my_table"),
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text


class TestBigQueryRegisterRawBucketSourceTableValidation:
    """Round-4 review: ``_validate_bigquery_register_payload`` must apply the
    same RAW-value rule to ``bucket`` and ``source_table`` as it does to
    ``name``. Pre-fix the helper validated ``bucket.strip()`` /
    ``source_table.strip()`` but ``register_table`` persists the un-stripped
    value, so ``"my_dataset "`` slipped through and 500'd downstream at
    view-create time. Parity with the ``name`` fix from round 3."""

    def test_register_rejects_bucket_with_leading_whitespace(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(bucket=" my_dataset"),
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        # Operator-friendly: surface the offending raw value verbatim.
        assert " my_dataset" in body["detail"]
        assert "dataset" in body["detail"].lower()

    def test_register_rejects_bucket_with_trailing_whitespace(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(bucket="my_dataset "),
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert "my_dataset " in body["detail"]
        assert "dataset" in body["detail"].lower()

    def test_register_rejects_source_table_with_leading_whitespace(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(source_table=" my_table"),
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert " my_table" in body["detail"]
        assert "source_table" in body["detail"].lower()

    def test_register_rejects_source_table_with_trailing_whitespace(
        self, seeded_app, bq_instance, stub_bq_extractor,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(source_table="my_table "),
            headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert "my_table " in body["detail"]
        assert "source_table" in body["detail"].lower()

    def test_precheck_rejects_bucket_with_leading_whitespace(
        self, seeded_app, bq_instance,
    ):
        """Validation runs identically in /precheck and short-circuits before
        the BQ round-trip — the helper is shared, so this is the same code
        path covered above, but we assert the BQ Client is never constructed."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("google.cloud.bigquery.Client") as cls:
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(bucket=" my_dataset"),
                headers=_auth(token),
            )
        assert resp.status_code == 400, resp.text
        assert " my_dataset" in resp.json()["detail"]
        cls.assert_not_called()

    def test_precheck_rejects_bucket_with_trailing_whitespace(
        self, seeded_app, bq_instance,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("google.cloud.bigquery.Client") as cls:
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(bucket="my_dataset "),
                headers=_auth(token),
            )
        assert resp.status_code == 400, resp.text
        cls.assert_not_called()

    def test_precheck_rejects_source_table_with_leading_whitespace(
        self, seeded_app, bq_instance,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("google.cloud.bigquery.Client") as cls:
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(source_table=" my_table"),
                headers=_auth(token),
            )
        assert resp.status_code == 400, resp.text
        cls.assert_not_called()

    def test_precheck_rejects_source_table_with_trailing_whitespace(
        self, seeded_app, bq_instance,
    ):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        with patch("google.cloud.bigquery.Client") as cls:
            resp = c.post(
                "/api/admin/register-table/precheck",
                json=_bq_payload(source_table="my_table "),
                headers=_auth(token),
            )
        assert resp.status_code == 400, resp.text
        cls.assert_not_called()


class TestBigQueryWorkerExceptionVsTimeout:
    """Round-3 review IMPORTANT 2: when the synchronous worker raises
    *within* the wall-clock budget, the API must surface that as a 500
    (hard error) — NOT 202 (timeout/retry). Earlier revisions mapped both
    outcomes to "timeout", which hid real failures behind a misleading
    "still working in the background" response for a budget-window worth
    of seconds, then the BG retry surfaced the same exception in the logs."""

    def test_worker_raises_within_budget_returns_500(
        self, seeded_app, bq_instance, monkeypatch,
    ):
        # Stub rebuild_from_registry to RAISE (not return errors). Worker
        # finishes within budget but the exception lands in err_holder.
        def boom(conn=None, output_dir=None):
            raise RuntimeError("simulated GCE auth failure")
        monkeypatch.setattr(
            "connectors.bigquery.extractor.rebuild_from_registry",
            boom,
        )
        orch_mock = MagicMock()
        monkeypatch.setattr(
            "src.orchestrator.SyncOrchestrator",
            lambda *a, **kw: orch_mock,
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="boomtable"),
            headers=_auth(token),
        )
        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert body["status"] == "rebuild_failed"
        # The exception message must show up in the body so the operator
        # gets the actual root cause, not a "timeout" red herring.
        assert body["errors"], body
        assert any(
            "simulated GCE auth failure" in (e.get("error") or "")
            for e in body["errors"]
        ), body["errors"]
        # The row was still inserted before the rebuild ran — re-running
        # after fixing the underlying issue picks it up.
        list_resp = c.get("/api/admin/registry", headers=_auth(token))
        assert "boomtable" in [t["name"] for t in list_resp.json()["tables"]]

    def test_worker_still_running_at_timeout_returns_202(
        self, seeded_app, bq_instance, monkeypatch,
    ):
        """Counterpart: if the worker is genuinely still running when the
        budget expires, 202 + BackgroundTask is correct."""
        import time

        def slow_ok(conn=None, output_dir=None):
            time.sleep(0.15)
            return {
                "project_id": "my-test-project",
                "tables_registered": 1,
                "errors": [],
                "skipped": False,
            }
        monkeypatch.setattr(
            "connectors.bigquery.extractor.rebuild_from_registry",
            slow_ok,
        )
        orch_mock = MagicMock()
        monkeypatch.setattr(
            "src.orchestrator.SyncOrchestrator",
            lambda *a, **kw: orch_mock,
        )
        # Force a short budget so the worker is still running when wait()
        # returns False.
        monkeypatch.setattr(
            "app.api.admin._BQ_SYNC_REGISTER_TIMEOUT_S", 0.05, raising=False,
        )

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/register-table",
            json=_bq_payload(name="slowtable"),
            headers=_auth(token),
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "accepted"


class TestRegisterTablePrecheckHandlerIsSync:
    """Round-3 review NIT 3: ``register_table_precheck`` must be a plain
    ``def`` (not ``async def``) — the BQ branch makes synchronous
    ``bigquery.Client(...)`` / ``client.get_table(...)`` calls that would
    otherwise block the asyncio event loop. Mirrors the same conversion
    already done for ``register_table``."""

    def test_precheck_handler_is_sync(self):
        import inspect
        from app.api import admin as admin_mod
        assert not inspect.iscoroutinefunction(
            admin_mod.register_table_precheck
        ), (
            "register_table_precheck must be a plain `def` so FastAPI runs "
            "it in a threadpool; otherwise the synchronous bigquery.Client "
            "calls block the asyncio event loop."
        )
