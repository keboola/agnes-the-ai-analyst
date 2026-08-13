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

        # Per-card default + token-rotation controls, ported from the
        # now-retired Keboola section of /admin/datasource-credentials. They
        # live in the card's Actions menu and settings body now rather than in
        # a standing button strip, but every one of them is still reachable —
        # the redesign moved them, it did not drop them.
        assert "setDefaultConn" in body
        assert "Make default project" in body
        assert "toggleRotate" in body
        assert "Rotate storage token" in body
        assert 'class="ds-rotate-row"' in body

        # Master (owner) token controls — separate vault slot consumed by the
        # semantic-layer sync (task 8, #contract in Task 3). Labelled by what
        # it is FOR; the Keboola noun stays in the hint.
        assert "saveMasterToken" in body
        assert "removeMasterToken" in body
        assert "Semantic-layer token" in body
        assert "MASTER (owner) token" in body
        assert 'kind: "master"' in body

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


class TestDataSourcesPageCarriesNoSemanticStatusStrip:
    """No page-wide semantic-layer status band above the sources list.

    It used to render one — 'Semantic layer: Never synced yet.' / '… — OK.' /
    '… failed: <result>' at full width, in all three states (task 7 slim-down,
    #953 status visibility). The Data section now carries **Semantic layer as
    its own tab** in the same tab row, and every source card already carries a
    semantic cell in its pipeline strip, so the band was a third copy of the
    same fact — occupying the position directly above the list, which is the
    one an admin's eye lands on first.

    These invert the three cases the band was pinned for: whatever the recorded
    sync state, none of its sentences may reach this page. The Semantic layer
    DESTINATION must survive — removing the band must not remove the way there.
    """

    #: The band's own copy, per state. Absence of these is the contract; the
    #: bare words "Semantic layer" and "Never synced" stay legal, since the tab
    #: row and the per-card pipeline cells both use them.
    _BAND_COPY = ("Never synced yet.", "Last sync attempt", "— OK.")

    def _body(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/data-sources", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        return resp.text

    def test_no_band_when_nothing_synced_yet(self, seeded_app):
        body = self._body(seeded_app)
        for copy in self._BAND_COPY:
            assert copy not in body
        assert "/admin/semantic-layer" in body

    def test_no_band_after_failed_sync(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import _record_completion

        _record_completion("error", "needs a master token")

        body = self._body(seeded_app)
        for copy in self._BAND_COPY:
            assert copy not in body
        # The failure detail belonged to the band — it must not leak onto the
        # page some other way now that the band is gone.
        assert "needs a master token" not in body
        assert "/admin/semantic-layer" in body

    def test_no_band_after_successful_sync(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import _record_completion

        _record_completion("ok", {"status": "ok", "created_or_updated": 0, "pruned": 0})

        body = self._body(seeded_app)
        for copy in self._BAND_COPY:
            assert copy not in body
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
        # The steps strip names them in flow order. It is the SHARED drawer's
        # strip now (`css/drawer.css`) rather than a page-local `.ds-wsteps` —
        # same four steps, same order, one sheet with the group drawer.
        strip = body.split('class="ds-drawer__steps"', 1)[1].split("</div>", 1)[0]
        for label in ("Connect", "Choose tables", "Bundle", "Share"):
            assert label in strip

    def test_the_steps_are_buttons_and_only_connect_starts_reachable(self, seeded_app):
        """The strip is the way BACK into a step you have already been
        through, so a step has to be a real control — and a step you have not
        earned has to say so in its own state rather than by a click that
        does nothing. Served state: step 1 live, 2-4 disabled."""
        body = self._page(seeded_app)
        strip = body.split('class="ds-drawer__steps"', 1)[1].split("</div>", 1)[0]
        assert strip.count("<button") == 4, "every step must be a control"
        # aria-hidden would take the whole strip out of the a11y tree — it is
        # a navigation control now, not decoration.
        assert "aria-hidden" not in strip
        assert strip.count("disabled") == 3, "only Connect is reachable at open"
        assert 'data-wstep="1"' in strip and "is-now" in strip

    def test_the_table_picker_collapses_and_selects_by_bucket(self, seeded_app):
        """A real project lists dozens of buckets and hundreds of tables. The
        picker's unit of navigation is the bucket — closed by default, with
        its own tri-state checkbox as the bulk gesture — and the per-table
        checkboxes stay for the granular case. Pinning the renderer's
        contract, since the picker itself is built client-side."""
        body = self._page(seeded_app)
        # Bucket chrome: a toggle that reports its own expanded state, a
        # bucket-level checkbox, and the per-bucket selected/total read-out.
        for marker in (
            "ds-bucket-toggle",
            "ds-bucket-check",
            "data-bucket-group",
            "data-bucket-meta",
            'aria-expanded="${openByDefault}"',
        ):
            assert marker in body, marker
        # Closed unless the project has exactly one bucket, where there is
        # nothing to choose between.
        assert "const openByDefault = buckets.length === 1;" in body
        # Granular selection is untouched.
        assert "ds-table-checkbox" in body
        # Scale controls: filter, bulk select/clear, expand-all, and a count
        # that is over the WHOLE picker rather than over what the filter shows.
        for marker in (
            "data-picker-search",
            "data-picker-all",
            "data-picker-none",
            "data-picker-expand",
            "data-picker-count",
            "hidden by the filter",
        ):
            assert marker in body, marker

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


class TestSourcesIsEveryConnector:
    """Sources means every source.

    The page used to say "Keboola projects", offer "+ Add Keboola project",
    and fetch `?source_type=keboola` — while the drawer behind that button
    registered BigQuery, CSV and Jira tables that then appeared nowhere on
    it. Three surfaces disagreeing about what a source is, on the page whose
    whole job is to teach that model. These pin the fix from both ends: the
    server synthesizes a card for a connector that keeps no connection row,
    and the client asks for every type rather than one.
    """

    def test_a_connector_with_no_connection_row_still_gets_a_card(self, seeded_app):
        """A registered BigQuery table means a BigQuery source exists, whether
        or not anything wrote a `source_connections` row for it."""
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import table_registry_repo

        tid = f"bqderived-{uuid.uuid4().hex[:6]}"
        table_registry_repo().register(
            id=tid,
            name=f"bq_derived_{tid[-6:]}",
            source_type="bigquery",
            bucket="analytics",
            source_table="derived",
            query_mode="remote",
        )
        try:
            inv = _source_inventory()
            card = next((d for d in inv["derived"] if d["source_type"] == "bigquery"), None)
            assert card is not None, "a registered BigQuery table must surface a BigQuery card"
            assert card["derived"] is True
            # It says where it is REALLY managed rather than offering controls
            # this page does not own.
            assert card["settings_href"] == "/admin/datasource-credentials"
            # And its tables are attributed to it, not reported as orphans.
            assert inv["pipelines"][card["id"]]["tables"]["count"] >= 1
        finally:
            table_registry_repo().unregister(tid)

    def test_a_derived_cards_strip_is_its_own_connectors(self, seeded_app):
        """Per-connector cells, not a fixed four: BigQuery is queried live, so
        "never synced" is its permanent and useless state — it carries the cost
        guard instead, and never a semantic cell."""
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import table_registry_repo

        tid = f"bqcost-{uuid.uuid4().hex[:6]}"
        table_registry_repo().register(
            id=tid,
            name=f"bq_cost_{tid[-6:]}",
            source_type="bigquery",
            bucket="analytics",
            source_table="cost",
            query_mode="remote",
        )
        try:
            inv = _source_inventory()
            card = next(d for d in inv["derived"] if d["source_type"] == "bigquery")
            cells = inv["pipelines"][card["id"]]
            assert "cost" in cells and "semantic" not in cells
            # The caps are read from live config and rendered as the operator
            # wrote them, so a raised cap shows the raised number.
            assert cells["cost"]["scan"].endswith("GiB")
            assert cells["cost"]["materialize"].endswith("GiB")
        finally:
            table_registry_repo().unregister(tid)

    def test_a_real_connection_of_that_type_owns_the_card_instead(self, seeded_app):
        """A derived card is a fallback, not a duplicate — a stored BigQuery
        connection means the page must show that one and not both."""
        import uuid

        from app.web.router import _source_inventory
        from src.repositories import source_connections_repo

        conn_id = f"bqreal-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name=f"BQ Real {conn_id[-4:]}",
            source_type="bigquery",
            config={"project_id": "acme-warehouse"},
        )
        try:
            inv = _source_inventory()
            assert conn_id in inv["pipelines"]
            assert not [d for d in inv["derived"] if d["source_type"] == "bigquery"]
        finally:
            source_connections_repo().delete(conn_id)

    def test_the_page_asks_for_every_source_type(self, seeded_app):
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        # The Keboola-only filter is gone, and the derived cards ride along.
        assert "source_type=keboola" not in body
        assert "DERIVED_SOURCES" in body
        # One heading, one CTA, both connector-agnostic.
        assert "Connected sources" in body
        assert "+ Add source" in body
        assert "Keboola projects" not in body


class TestSourceCardHierarchy:
    """The card ranks its contents instead of stacking five equal bands.

    Head (identity + one status word + the verbs) → strip → a settings body
    that is CLOSED. These pin the two properties that make that safe: the
    status word is a fold over the strip rather than a sixth fact, and every
    control the old action strip carried is still reachable.
    """

    def test_the_status_word_folds_the_strip(self, seeded_app):
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert "_sourceHealth" in body
        for state in ("Healthy", "No tables yet", "Reaches nobody", "failing sync"):
            assert state in body
        # An absent semantic-layer token must NOT reach the card-level verdict:
        # it is opt-in, and an off feature is not a broken source.
        health = body[body.index("function _sourceHealth") : body.index("function _sourceSubtitle")]
        assert "semantic" not in health

    def test_the_body_is_closed_and_the_caret_says_so(self, seeded_app):
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert 'class="ds-src__body" id="ds-body-${id}" hidden' in body
        assert 'aria-expanded="false" aria-controls="ds-body-${id}"' in body
        # A verb reached from the menu opens the body it writes into —
        # otherwise the menu item appears to do nothing.
        assert body.count("setSourceOpen(id, true)") >= 3

    def test_an_empty_source_offers_the_verb_not_a_dead_link(self, seeded_app):
        """A source with nothing registered must not point at /admin/tables —
        the list of tables that already exist is the one page that cannot fix
        an empty source. The cell becomes the action instead, which is also
        where the card's primary verb stays one click away."""
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        assert "Add the first tables" in body
        strip = body[body.index("function _pipelineStripHtml") : body.index("function _sourceHealth")]
        # A stored connection browses its own tables; a derived card has none
        # to browse, so it opens the wizard already on its connector.
        assert "toggleBrowse(" in strip
        assert "openWizard(" in strip

    def test_no_control_was_dropped_with_the_action_strip(self, seeded_app):
        c = seeded_app["client"]
        body = c.get(
            "/admin/data-sources",
            headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
        ).text
        for fn in (
            "testConn",
            "toggleBrowse",
            "toggleRotate",
            "toggleMasterToken",
            "toggleChatTools",
            "setDefaultConn",
            "unbindProject",
            "deleteConn",
        ):
            assert fn in body, f"{fn} lost its route into the UI"
        # Delete is separated and marked, never reachable by momentum from the
        # item above it.
        assert "apg-menu__item--danger" in body
        assert "apg-menu__sep" in body
