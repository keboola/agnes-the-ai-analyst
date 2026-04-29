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
