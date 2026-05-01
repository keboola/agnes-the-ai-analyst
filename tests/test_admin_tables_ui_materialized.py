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
    assert "discoverBqDatasets('editBqDatasetList')" in html
    assert "discoverBqTables('editBqDataset', 'editBqTableList')" in html
    assert "prefillFromTable('editBqSourceQuery')" in html
    assert 'id="editBqDatasetList"' in html
    assert 'id="editBqTableList"' in html
    assert 'list="editBqDatasetList"' in html
    assert 'list="editBqTableList"' in html


def test_admin_tables_keboola_branch_unchanged(seeded_app, monkeypatch):
    """Negative — when `data_source.type` is NOT bigquery, the BQ form
    fields don't appear at all (the Jinja `{% if %}` block guards them)."""
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
        assert 'id="bqEntityType"' not in html
        assert 'id="bqSourceQuery"' not in html
        # Keboola form's regBucket / regTableId still there.
        assert 'id="regTableId"' in html
        assert 'id="regBucket"' in html
    finally:
        reset_cache()
