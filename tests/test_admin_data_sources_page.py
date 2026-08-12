"""Tests for the /admin/data-sources "Add Keboola project" wizard page (#755).

Covers:
- Auth gate (admin loads, non-admin 403, unauthenticated redirect).
- Page-shell markers the JS hangs off.
- Vault-key-not-configured blocking banner + disabled affordance.
- Vault-key-configured: no banner, affordance enabled.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """`_refresh_state` is a module-level dict shared with the refresh
    endpoint tests — reset it around every test in this file too."""
    from app.api import keboola_semantic_layer_refresh as endpoint_module

    reset = {
        "run_id": None,
        "started_at": None,
        "last_completed_at": None,
        "last_status": None,
        "last_result": None,
    }
    endpoint_module._refresh_state.update(reset)
    yield
    endpoint_module._refresh_state.update(reset)


class TestDataSourcesPageAuth:
    def test_admin_can_load_page(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/data-sources", headers={"Accept": "text/html"})
        finally:
            c.cookies.clear()
            _reset_ephemeral_key_for_tests()
        assert resp.status_code == 200, resp.text
        body = resp.text

        # Hero + nav-distinguishing copy (#755 acceptance: data vs MCP sources
        # legible from the page itself).
        assert "Data sources" in body
        assert "/admin/mcp-sources" in body

        # Page-shell markers the JS targets.
        assert 'id="ds-add-btn"' in body
        assert 'id="ds-conn-list"' in body
        assert 'id="ds-wizard-overlay"' in body
        assert 'id="ds-new-stack"' in body
        assert 'id="ds-new-token"' in body

        # Endpoint constants — guards against URL drift between UI and API.
        assert "/api/admin/source-connections" in body
        assert "/api/admin/register-table" in body

        # Per-card "Set as default" + "Rotate token" controls, ported from
        # the now-retired Keboola section of /admin/datasource-credentials.
        assert "setDefaultConn" in body
        assert "Set as default" in body
        assert "toggleRotate" in body
        assert "Rotate token" in body
        assert 'class="ds-rotate-row"' in body

        # Master (owner) token controls — separate vault slot consumed by the
        # semantic-layer sync (task 8, #contract in Task 3).
        assert "saveMasterToken" in body
        assert "removeMasterToken" in body
        assert "Master token (semantic layer)" in body
        assert 'kind: "master"' in body
        assert "required only for semantic-layer sync (Metastore)" in body

        # Reciprocal link to the vault-secrets page.
        assert "/admin/datasource-credentials" in body

    def test_non_admin_cannot_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauthenticated_redirects(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/data-sources", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)


class TestDataSourcesPageVaultBanner:
    def test_banner_shown_when_vault_key_unset(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/data-sources")
        finally:
            c.cookies.clear()
            _reset_ephemeral_key_for_tests()
        assert resp.status_code == 200
        body = resp.text
        assert "Vault key not configured" in body
        assert "AGNES_VAULT_KEY" in body
        # The "add" flow is disabled without a vault key.
        assert 'id="ds-add-btn" disabled' in body

    def test_no_banner_when_vault_key_set(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
        _reset_ephemeral_key_for_tests()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        c.cookies.set("access_token", token)
        try:
            resp = c.get("/admin/data-sources")
        finally:
            c.cookies.clear()
            _reset_ephemeral_key_for_tests()
        assert resp.status_code == 200
        body = resp.text
        assert "Vault key not configured" not in body
        assert 'id="ds-add-btn" disabled' not in body


class TestDataSourcesPageSemanticLayerSummary:
    """Slim one-line status ('Semantic layer: <status> — manage at
    /admin/semantic-layer') — task 7 moved the per-source counts, the
    "Sync now" control and the orphaned-rows view to /admin/semantic-layer.
    This page keeps just enough to tell an admin whether the last sync
    succeeded and where to go for detail.

    #953: the line must always show SOME state (never-synced / ok / error),
    not just after a successful sync — an admin who hasn't synced yet, or
    whose last attempt failed, needs to see that too.
    """

    def test_never_synced_state_shown_when_nothing_synced_yet(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Semantic layer" in body
        assert "never" in body.lower()
        assert "/admin/semantic-layer" in body

    def test_error_state_shown_after_failed_sync(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import _record_completion

        _record_completion("error", "needs a master token")

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Semantic layer" in body
        assert "failed" in body.lower()
        assert "needs a master token" in body
        assert "/admin/semantic-layer" in body

    def test_ok_state_shown_after_successful_sync(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import _record_completion

        _record_completion("ok", {"status": "ok", "created_or_updated": 0, "pruned": 0})

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Semantic layer" in body
        assert "OK" in body
        assert "/admin/semantic-layer" in body


class TestWizardRegisterPayloadContract:
    """The wizard's register payload must send the BARE in-bucket name.

    The pre-fix wizard sent the full Keboola table id (`bucket.table`) as
    `source_table`; combined with the separate `bucket` field, the sync path
    re-composed `bucket.bucket.table` — every wizard-registered table then
    failed to materialize (doubled prefix → nonexistent table id upstream)
    and the catalog preview showed "not found". Text-assertion contract on
    the template so a future edit can't silently regress the payload.
    """

    @staticmethod
    def _template_text():
        from pathlib import Path

        tpl = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
        return tpl.read_text(encoding="utf-8")

    def test_register_payload_uses_bare_table_name(self):
        tpl = self._template_text()
        assert 'data-table-bare="${_esc(bare)}"' in tpl
        assert "cb.dataset.tableBare || cb.dataset.tableName" in tpl
        # The regression: full table id sent as source_table.
        assert "source_table: cb.dataset.tableId" not in tpl

    def test_scoped_token_note_wired(self):
        """Bucket-scoped tokens get a partial listing — the picker must say so."""
        tpl = self._template_text()
        assert 'data.scope === "token_buckets"' in tpl
        assert "ds-scope-note" in tpl


class TestAddDataWizard:
    """The 4-step Add-data flow (connect → choose tables → bundle → share) —
    the redesign's one path from "nothing connected" to "data in someone's
    hands" (spec §3.6–3.8), grown out of the two-step Add-Keboola-project
    modal without dropping any of its behavior.

    The flow itself is client JS over endpoints that carry their own suites
    (source-connections, register-table, data-packages, grants); what this
    class pins is the SERVED SCAFFOLDING — the pieces whose silent absence
    would degrade the wizard back to the old two steps without failing
    anything else."""

    def _page(self, seeded_app) -> str:
        c = seeded_app["client"]
        return c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text

    def test_the_four_steps_are_declared(self, seeded_app):
        body = self._page(seeded_app)
        for pane in (
            "ds-wizard-step-connect",
            "ds-wizard-step-tables",
            "ds-wizard-step-bundle",
            "ds-wizard-step-share",
        ):
            assert pane in body
        # The steps strip names them in flow order.
        strip = body.split('class="ds-wsteps"', 1)[1].split("</div>", 1)[0]
        for label in ("Connect", "Choose tables", "Bundle", "Share"):
            assert label in strip

    def test_semantic_layer_opt_in_lives_on_connect(self, seeded_app):
        """The master-token requirement used to be discoverable only on the
        semantic-layer page's empty state; the wizard offers it at the moment
        of connecting, skippably."""
        body = self._page(seeded_app)
        assert 'id="ds-new-semantic"' in body
        assert 'id="ds-new-master"' in body
        assert "owner" in body  # the copy says WHICH token this is

    def test_bundle_and_share_write_through_the_canonical_apis(self, seeded_app):
        """The wizard must create real packages and real grants — the same
        rows /admin/data-packages and a group's Access tab edit — never a
        parallel store."""
        body = self._page(seeded_app)
        assert "/api/admin/data-packages" in body
        assert "/api/admin/grants" in body
        assert "/api/admin/groups" in body

    def test_old_register_only_exit_is_preserved(self, seeded_app):
        """The pre-redesign behavior — register the selection and stop — is
        an explicit escape hatch, not removed."""
        body = self._page(seeded_app)
        assert 'id="ds-wizard-finish-early-btn"' in body

    def test_share_step_is_skippable(self, seeded_app):
        body = self._page(seeded_app)
        assert 'id="ds-wizard-skip-btn"' in body
        # The lede says WHY skipping is safe (the Overview catches it).
        assert "waits for you on the Overview" in body

    def test_add_deep_link_auto_opens(self, seeded_app):
        """The Overview's '+ Add data' lands on ?add=1 — the page script must
        read it, or the button silently becomes a plain nav link."""
        body = self._page(seeded_app)
        assert 'has("add")' in body

    def test_overview_carries_the_add_data_action(self, seeded_app):
        c = seeded_app["client"]
        hub = c.get(
            "/admin",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert "/admin/data-sources?add=1" in hub


class TestAddDataConnectorPicker:
    """Step 1 opens on a connector picker (spec §3.7): Keboola and BigQuery
    are real flows; CSV and Jira are HONEST guidance — no CSV connector
    exists (files enter through Library Collections) and Jira is configured
    instance-side + webhook. Only step 1 varies by source; Bundle and Share
    are shared."""

    def _page(self, seeded_app) -> str:
        c = seeded_app["client"]
        return c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text

    def test_all_four_connectors_are_offered(self, seeded_app):
        body = self._page(seeded_app)
        for src in ("keboola", "bigquery", "csv", "jira"):
            assert f'data-wsrc="{src}"' in body
            assert f'data-wsrcform="{src}"' in body

    def test_bigquery_reads_the_real_credential_status(self, seeded_app):
        """BQ credentials are the instance service account — the wizard must
        consult /api/admin/datasource-secrets and route to Instance secrets,
        never grow its own credential field."""
        body = self._page(seeded_app)
        assert "/api/admin/datasource-secrets" in body
        assert "BIGQUERY_SERVICE_ACCOUNT_JSON" in body
        assert "/admin/datasource-credentials" in body

    def test_bigquery_registers_live_queries_and_defers_depth(self, seeded_app):
        """The wizard registers `remote` rows; saved queries / partitioning
        keep their full editor on /admin/tables — stated, not dropped."""
        body = self._page(seeded_app)
        assert '"remote"' in body.split("_registerBqRows", 1)[1].split("async function", 1)[0]
        assert "/admin/tables" in body

    def test_csv_guidance_is_honest_about_the_missing_connector(self, seeded_app):
        """`csv` is an alias with no connector (docs/DATA_SOURCES.md); the
        card must send people to Library Collections, not fake a form."""
        body = self._page(seeded_app)
        assert "collections-vs-data-packages" in body

    def test_existing_connection_shortcut_is_offered(self, seeded_app):
        """The wizard is also how MORE tables are added from a project that
        is already connected — without this, 'Add data' reads as 'new
        connection only'."""
        body = self._page(seeded_app)
        assert 'id="ds-wexisting-select"' in body
        assert 'id="ds-wexisting-btn"' in body


class TestSourcePipelineStrip:
    """The per-source pipeline strip — connected → synced → semantic →
    feeding whom, on one card (spec §3.7).

    Its whole value is that each cell is TRUE and actionable, so that is
    what these pin: the shape is per-connector (no semantic cell where there
    is no Metastore), a cell degrades rather than lying, and unattributable
    legacy rows are named as unlinked instead of reported as "no tables yet"
    — the misreading that made an eleven-table instance look empty.
    """

    def test_strip_is_computed_per_connection(self, seeded_app):
        import uuid

        from src.repositories import source_connections_repo
        from app.web.router import _source_pipelines

        conn_id = f"probe-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"Pipeline Probe {conn_id[-4:]}",
            source_type="keboola",
            config={"stack_url": "https://connection.keboola.com"},
        )
        try:
            strips = _source_pipelines()
            assert conn_id in strips
            cells = strips[conn_id]
            assert set(cells) == {"tables", "sync", "semantic", "feeds"}
            # Nothing registered or granted yet — every cell says so rather
            # than guessing.
            assert cells["tables"]["count"] == 0
            assert cells["sync"]["last_sync"] is None
            assert cells["semantic"]["token"] is False
            assert cells["feeds"]["packages"] == 0
        finally:
            source_connections_repo().delete(conn_id)

    def test_bigquery_source_has_no_semantic_cell(self, seeded_app):
        """The strip is per-connector: the Metastore is a Keboola API, so a
        BigQuery source must not render a semantic cell at all (rather than
        an empty or misleading one)."""
        from src.repositories import source_connections_repo
        from app.web.router import _source_pipelines

        import uuid

        conn_id = f"bqprobe-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"BQ Probe {conn_id[-4:]}",
            source_type="bigquery",
            config={"project_id": "acme-warehouse"},
        )
        try:
            cells = _source_pipelines()[conn_id]
            assert "semantic" not in cells
            assert {"tables", "sync", "feeds"}.issubset(cells)
        finally:
            source_connections_repo().delete(conn_id)

    def test_a_registered_table_is_attributed_to_its_connection(self, seeded_app):
        import uuid

        from src.repositories import source_connections_repo, table_registry_repo
        from app.web.router import _source_pipelines

        conn_id = f"attrprobe-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"Attributed Probe {conn_id[-4:]}",
            source_type="keboola",
            config={"stack_url": "https://connection.keboola.com"},
        )
        tid = f"strip-{uuid.uuid4().hex[:6]}"
        table_registry_repo().register(
            id=tid,
            name=f"strip_table_{tid[-6:]}",
            source_type="keboola",
            bucket="in.c-test",
            source_table="strip",
            query_mode="local",
            connection_id=conn_id,
        )
        try:
            cells = _source_pipelines()[conn_id]
            assert cells["tables"]["count"] == 1
            assert cells["tables"]["basis"] == "connection"
            # Registered but in no package — the end of the chain says so.
            assert cells["feeds"]["packages"] == 0
        finally:
            table_registry_repo().unregister(tid)
            source_connections_repo().delete(conn_id)

    def test_the_page_serves_the_strip_data(self, seeded_app):
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert "SOURCE_PIPELINES" in body
        assert "_pipelineStripHtml" in body
        # Each cell routes to the page owning that stage.
        for href in ("/admin/tables", "/admin/sync", "/admin/semantic-layer", "/admin/data-packages"):
            assert href in body
