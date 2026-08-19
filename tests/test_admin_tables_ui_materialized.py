"""`/admin/tables` register modal exposes the BQ Type selector + Custom SQL.

The backend supports `query_mode='materialized'` since v0.25.0. The Jinja
template at `app/web/templates/admin_tables.html` exposes it via an
operator-facing **Type** selector (Table / View / Custom SQL Query) that
maps to query_mode in the payload (Table+View → remote, Query → materialized).

Structural-only test (no headless browser): loads the template through the
running app and asserts the expected element ids + attributes are present
in the rendered HTML for a `data_source_type='bigquery'` deployment.
"""

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bq_instance(monkeypatch):
    """Force `data_source.type='bigquery'` so /admin/tables renders the BQ
    branch of the register modal."""
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {"project": "my-test-project", "location": "us"},
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    yield fake_cfg
    reset_cache()


def test_admin_tables_renders_two_question_radio_form(seeded_app, bq_instance):
    """Q1 = how should analysts access this data? (live / synced).
    Q2 = (only when synced) what to sync? (whole / custom).
    Replaces the earlier flat 4-option dropdown that mixed source-kind +
    distribution-mode into one selector — both UX reviewers (info-arch +
    analyst persona) flagged the conflation as the core confusion."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    html = r.text

    # Q1 radio group.
    assert 'name="bqAccessMode"' in html
    assert 'value="live"' in html
    assert 'value="synced"' in html
    assert "onBqAccessModeChange" in html

    # Q2 radio group (conditional on Q1).
    assert 'name="bqSyncMode"' in html
    assert 'value="whole"' in html
    assert 'value="custom"' in html
    assert "onBqSyncModeChange" in html

    # Custom-SQL textarea + "Use table as base" prefill button.
    assert 'id="bqSourceQuery"' in html
    assert "prefillFromTable" in html
    assert "bq-source-custom" in html

    # Table/dataset inputs reused across live + synced/whole.
    assert 'id="bqDataset"' in html
    assert 'id="bqSourceTable"' in html
    assert "bq-source-table" in html
    assert "bq-access-synced" in html

    # Discover + List tables buttons.
    assert "discoverBqDatasets" in html
    assert "discoverBqTables" in html

    # No leftover jargon labels from the prior Type-selector iterations.
    assert "Direct query" not in html
    assert "Sync to parquet" not in html

    # Vendor-agnostic — no internal issue refs in operator-facing UI text.
    assert "Milestone 2" not in html
    assert "issue #108" not in html

    # Package-centric rewrite: connector tabs were dropped. The BQ
    # register modal stays in DOM as a top-level overlay reachable from
    # the `+ Register new table ▾` action-bar dropdown. Anchor the
    # field-scope check on the modal id instead of the deleted
    # tab-content section.
    bq_modal_start = html.index('id="registerBqModal"')
    bq_modal_end = html.index("</div>\n            </div>", bq_modal_start)
    bq_modal = html[bq_modal_start:bq_modal_end]
    assert 'name="bqAccessMode"' in bq_modal
    assert 'id="bqDataset"' in bq_modal
    assert 'id="bqSourceQuery"' in bq_modal


def test_edit_modal_has_bq_parity_fields(seeded_app, bq_instance):
    """Edit modal mirrors Register's two-question radio model (Q1 access
    mode: live/synced; Q2 sync mode: whole/custom). Pre-fix Edit had only
    sync_strategy+primary_key+description+folder — missing all BQ-specific
    edit surface. Operator now can flip access mode, change dataset/table,
    rewrite SQL, and tweak the schedule without dropping & re-adding."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    html = r.text

    # Edit Q1 + Q2 radios.
    assert 'name="editBqAccessMode"' in html
    assert 'name="editBqSyncMode"' in html
    assert "onEditBqAccessModeChange" in html
    assert "onEditBqSyncModeChange" in html

    # BQ-specific edit fields.
    assert 'id="editBqDataset"' in html
    assert 'id="editBqSourceTable"' in html
    assert 'id="editBqSourceQuery"' in html
    assert 'id="editBqSyncSchedule"' in html

    # Visibility classes for adaptive show/hide on access/sync mode switch.
    assert "bq-edit-access-synced" in html
    assert "bq-edit-source-table" in html
    assert "bq-edit-source-custom" in html

    # Mode-switch warning surface (filled by JS when operator flips access
    # mode mid-edit).
    assert 'id="editBqModeWarning"' in html

    # Source-type badge so the JS branch knows whether to render BQ vs
    # Keboola fields without a second round-trip.
    assert 'id="editSourceTypeBadge"' in html

    # No leftover Type-selector remnants.
    assert 'id="editBqEntityType"' not in html
    assert "onEditBqTypeChange" not in html

    # Edit modal has the same Discover / List tables / Use-as-base buttons
    # as Register so the operator can re-pick the source from autocomplete
    # without dropping the row.
    assert "discoverBqDatasets(this, 'editBqDatasetList')" in html
    assert "discoverBqTables(this, 'editBqDataset', 'editBqTableList')" in html
    assert "prefillFromTable('editBqSourceQuery')" in html
    assert 'id="editBqDatasetList"' in html
    assert 'id="editBqTableList"' in html
    assert 'list="editBqDatasetList"' in html
    assert 'list="editBqTableList"' in html


def test_keboola_register_form_has_three_question_radio(seeded_app, monkeypatch):
    """Phase G (v26): Keboola tab Register form gains a third radio option
    'Direct extract (Storage API)' alongside the existing 'whole' and
    'custom' modes.

    - whole / custom → query_mode='materialized' (DuckDB Keboola extension)
    - direct → query_mode='local' + v26 sync_strategy panel
      (incremental / partitioned / full_refresh + where_filters)

    Phase F asserted `kbStrategy` was removed; v26 re-adds it inside the
    Direct-extract panel (visible only when 'direct' is selected).
    """
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/admin/tables", headers=_auth(token))
        html = r.text
        # Package-centric rewrite: anchor on the Keboola register modal id
        # (the connector tab that used to wrap this form is gone).
        kb_modal_start = html.index('id="registerKeboolaModal"')
        # The modal's outer wrapper closes via "</div>\n            </div>"
        # (modal -> modal-overlay). Use a generous slice + sanity-bound it
        # to the next modal-overlay id so we don't bleed into editKeboolaModal.
        next_modal_idx = html.find('id="editKeboolaModal"', kb_modal_start)
        kb_tab = html[kb_modal_start:next_modal_idx] if next_modal_idx > 0 else html[kb_modal_start:]

        # All three radios present.
        assert 'name="kbSyncMode"' in kb_tab
        assert 'value="whole"' in kb_tab
        assert 'value="custom"' in kb_tab
        assert 'value="direct"' in kb_tab

        # Bucket + source-table inputs reused for whole + direct modes.
        assert 'id="kbBucket"' in kb_tab
        assert 'id="kbSourceTable"' in kb_tab
        # Custom-SQL textarea + Use-table-as-base prefill button.
        assert 'id="kbSourceQuery"' in kb_tab
        assert "kbPrefillFromTable" in html or "prefillFromKeboolaTable('kbSourceQuery')" in html

        # Sync Schedule input.
        assert 'id="kbSyncSchedule"' in kb_tab

        # v26: Sync Strategy dropdown re-added (inside the Direct-extract panel)
        assert 'id="kbStrategy"' in kb_tab
        assert 'class="form-group kb-direct-only"' in kb_tab or "kb-direct-only" in kb_tab

        # Primary Key — under <details>Advanced.
        assert 'id="kbPrimaryKey"' in kb_tab
        assert "<details" in kb_tab
        assert ">Advanced" in kb_tab

        # Discover datasets / List tables buttons.
        assert "kbDiscoverBuckets" in html or "discoverKeboolaBuckets(" in html
        assert "kbListTables" in html or "discoverKeboolaTables(" in html
    finally:
        reset_cache()


def test_keboola_register_payload_maps_to_materialized(seeded_app, monkeypatch):
    """The form's whole-table mode posts query_mode='materialized' — Keboola
    materialized uses bucket/source_table (no SQL source_query)."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        auth = {"Authorization": f"Bearer {token}"}
        r = c.post(
            "/api/admin/register-table",
            headers=auth,
            json={
                "name": "orders",
                "source_type": "keboola",
                "query_mode": "materialized",
                "bucket": "in.c-sales",
                "source_table": "orders",
                "sync_schedule": "every 6h",
            },
        )
        assert r.status_code == 201, r.text
    finally:
        reset_cache()


def test_keboola_edit_modal_parity(seeded_app, monkeypatch):
    """Phase G (v26): Edit modal mirrors Register's three-question structure
    (whole | direct | custom) for Keboola rows.

    Phase F asserted `editKbStrategy` was removed; v26 re-adds it inside
    the Direct-extract panel for the same reason as the Register form."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/admin/tables", headers=_auth(token))
        html = r.text
        # Q2 radio in edit (now three modes).
        assert 'name="editKbSyncMode"' in html
        assert 'id="editKbBucket"' in html
        assert 'id="editKbSourceTable"' in html
        assert 'id="editKbSourceQuery"' in html
        assert 'id="editKbSyncSchedule"' in html
        # Discover/List/Use-as-base buttons mirror Register.
        assert "discoverKeboolaBuckets(this, 'editKbBucketList')" in html
        assert "discoverKeboolaTables(this, 'editKbBucket', 'editKbTableList')" in html
        assert "prefillFromKeboolaTable('editKbSourceQuery')" in html
        # v26: Strategy dropdown re-added inside Direct-extract panel
        assert 'id="editKbStrategy"' in html
        assert "editkb-direct-only" in html
        assert 'id="editKbPrimaryKey"' in html
    finally:
        reset_cache()


def test_bq_edit_modal_renders_as_dom_overlay(seeded_app, bq_instance):
    """Package-centric rewrite: the connector tab that used to wrap
    #editBqModal was dropped, but the modal itself stays in DOM as a
    top-level overlay reachable from the per-row Edit affordance. Old
    shared #editModal still exists but carries no BQ-specific fields."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    # BQ edit modal is in the DOM (as a top-level overlay now).
    assert 'id="editBqModal"' in html
    assert 'id="editBqDataset"' in html
    assert 'id="editBqSourceQuery"' in html
    # Old shared #editModal either gone or only carries non-BQ fields.
    if 'id="editModal"' in html:
        edit_modal_start = html.index('id="editModal"')
        # rough lookahead: scan until the next modal-overlay sibling or </body>
        edit_modal_end = (
            html.index('id="toast"', edit_modal_start) if 'id="toast"' in html[edit_modal_start:] else len(html)
        )
        edit_modal = html[edit_modal_start:edit_modal_end]
        assert 'id="editBqDataset"' not in edit_modal  # BQ fields aren't here anymore


def test_keboola_discover_buttons_disabled_on_bigquery_instance(seeded_app, monkeypatch):
    """C1 / #405, migrated for the one-signal fix (see
    test_admin_tables_connectedness.py): Discover/List/Use-as-base buttons
    in the Keboola tab render DISABLED with an explanatory tooltip (rather
    than being hidden) when Keboola is unreachable by BOTH routes — no
    `source_connections` registry row (none in this fixture) AND a
    non-keboola `data_source.type` scalar.

    Pre-fix this asserted on `data_source_type != 'keboola'` ALONE, which
    pinned the reported bug: it fired even on instances with a Keboola
    project connected through the registry, on the adjacent Sources tab.
    The guard now reads `connected_sources` (the sibling-shipped union of
    the registry + the legacy scalar + instance-side credential probes) —
    this test asserts the case where that list genuinely omits 'keboola',
    injected explicitly via `_inject_ctx` so the assertion doesn't
    silently depend on whether app/web/router.py has shipped the real key
    yet (`setdefault` — a real value always wins over the shim)."""
    from tests.test_admin_tables_connectedness import _inject_ctx

    fake_cfg = {"data_source": {"type": "bigquery", "bigquery": {"project": "p"}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    _inject_ctx(monkeypatch, connected_sources=["bigquery"])
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/admin/tables", headers=_auth(token))
        html = r.text
        # Inputs stay (manual entry works).
        assert 'id="kbBucket"' in html
        assert 'id="kbSourceTable"' in html
        # #405: the buttons now render (visible) but disabled, carrying a
        # tooltip that explains Keboola isn't connected.
        assert 'data-tooltip="Keboola not connected' in html
        # #347 follow-up: the tooltip's advice must be FOLLOWABLE — the old
        # copy pointed at a token field /admin/server-config never had.
        assert "connect a project in Data sources" in html
        assert "set token in Instance settings" not in html
        # The functional guarantee that actually matters: no live call sites
        # on an unreachable instance, so a click can never reach either
        # discover endpoint. Match the actual CALL SITES, not the function
        # definitions or JS comments that reference the names verbatim
        # (#347 moved several helpers out from under the keboola Jinja
        # guard, so they're defined as dead code on every instance).
        assert 'onclick="discoverKeboolaBuckets(' not in html
        assert 'onclick="discoverKeboolaTables(' not in html
        assert 'onclick="prefillFromKeboolaTable(' not in html
    finally:
        reset_cache()


def test_keboola_discover_buttons_visible_on_keboola_instance(seeded_app, monkeypatch):
    """Inverse — buttons render on a Keboola-typed instance."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/admin/tables", headers=_auth(token))
        html = r.text
        assert "discoverKeboolaBuckets" in html
        assert "discoverKeboolaTables" in html
        assert "prefillFromKeboolaTable" in html
    finally:
        reset_cache()


def test_keboola_test_connection_button_in_register_and_edit_modals(seeded_app):
    """#402: the Keboola register & edit modals expose a Test-connection
    button wired to the existing /api/admin/keboola/test-connection probe,
    with an inline result element and a self-contained onTestKeboola handler."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    html = c.get("/admin/tables", headers=_auth(token)).text
    # Button rendered in BOTH modal footers (register + edit).
    assert html.count('onclick="onTestKeboola(this)"') >= 2
    assert "Test connection" in html
    # Inline result element + handler defined + hits the existing endpoint.
    assert 'class="kbc-test-result"' in html
    assert "function onTestKeboola(" in html
    assert "/api/admin/keboola/test-connection" in html
    # #402 follow-up (Devin BUG-0001/0003): both modal-open paths clear the
    # test-result badge so a stale "ok" can't linger over a reopened blank form.
    assert "kbcResult.hidden = true" in html
    assert "editKbcResult.hidden = true" in html


def test_admin_tables_keboola_branch_unchanged(seeded_app, monkeypatch):
    """Phase E: the BQ form is always rendered (inside #tab-content-bigquery)
    regardless of data_source.type. On a Keboola instance the BQ tab is
    just hidden by default; the operator can still click into it. The
    legacy Type-selector remnant (#bqEntityType) must stay gone."""
    fake_cfg = {"data_source": {"type": "keboola", "keboola": {}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    try:
        r = c.get("/admin/tables", headers=_auth(token))
        assert r.status_code == 200, r.text
        html = r.text
        # Legacy Type-selector remnant must stay gone.
        assert 'id="bqEntityType"' not in html
        # BQ form now always rendered inside #tab-content-bigquery.
        assert 'id="bqSourceQuery"' in html
        # C3: legacy #registerModal removed; the Phase F Keboola modal
        # at #registerKeboolaModal owns the Keboola flow now.
        assert 'id="registerModal"' not in html
        assert 'id="kbBucket"' in html
        assert 'id="kbViewName"' in html
    finally:
        reset_cache()


def test_precheck_failure_pins_field_errors_for_two_step_connectors(seeded_app, bq_instance):
    """A 422 on a required field can only surface at PRECHECK for the
    two-step connectors, so the field marks have to be applied there.

    `register_table_precheck` runs "identical Pydantic validation to
    register-table" (its own docstring), and the BQ / Snowflake confirm step
    is reachable only after that precheck returned 200. Wiring
    `_applyFieldErrors` solely into `_confirmRegister{BigQuery,Snowflake}Table`
    therefore left `BQ_REGISTER_FIELD_MAP` / `SF_REGISTER_FIELD_MAP`
    unreachable for validation errors — the operator got the readable toast
    but never the red box that the rest of this feature promises, while the
    single-POST connectors (Keboola, Databricks) did.
    """
    c = seeded_app["client"]
    html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

    for fn, field_map in (
        ("function _registerBigQueryTable(", "BQ_REGISTER_FIELD_MAP"),
        ("function _registerSnowflakeTable(", "SF_REGISTER_FIELD_MAP"),
    ):
        start = html.index(fn)
        end = html.index("\n    function ", start + len(fn))
        body = html[start:end]
        assert "'/api/admin/register-table/precheck'" in body, (
            f"{fn} no longer calls precheck — this guard has nothing to check"
        )
        assert f"_applyFieldErrors(d && d.detail, {field_map})" in body, (
            f"{fn}: a precheck 422 must pin the field marks too, not just toast — "
            "the confirm step that carries the field map is unreachable until "
            "precheck has already passed validation"
        )


def test_nested_confirm_prompt_dialog_outranks_the_register_drawer(seeded_app, bq_instance):
    """`Use as base` inside a register drawer awaits an invisible dialog.

    modal.js's confirmModal / promptModal render a `.modal-backdrop` fixed at
    z-index 1000 (style-custom.css), but the register drawers here are
    `.ds-drawer` at 1200 (css/drawer.css) and `prefillFromTable` /
    `prefillFromKeboolaTable` are invoked from buttons *inside* them — so the
    dialog the flow then awaits painted behind the panel that opened it and
    the button looked dead. The page-scoped override must clear the drawer
    and stay under the toast.
    """
    import re
    from pathlib import Path

    c = seeded_app["client"]
    html = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

    def _z(css: str, selector: str) -> int:
        parts = css.split(selector + " {", 1)
        assert len(parts) == 2, f"no `{selector} {{` rule found — this guard has nothing to compare"
        m = re.search(r"z-index:\s*(\d+)", parts[1].split("}", 1)[0])
        assert m, f"{selector} lost its z-index — this guard has nothing to compare"
        return int(m.group(1))

    drawer_z = _z(Path("app/web/static/css/drawer.css").read_text(encoding="utf-8"), ".ds-drawer")
    backdrop_z = _z(html, ".modal-backdrop")
    toast_z = _z(html, ".toast")

    assert backdrop_z > drawer_z, (
        f".modal-backdrop ({backdrop_z}) must outrank .ds-drawer ({drawer_z}) — "
        "confirmModal/promptModal are opened from inside the register drawers"
    )
    assert backdrop_z < toast_z, f".modal-backdrop ({backdrop_z}) must stay under .toast ({toast_z})"
