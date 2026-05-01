"""UI tests for the per-connector tab layout."""
import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_admin_tables_renders_tab_nav(seeded_app):
    """Page has tab nav with at least the source types configured for
    the instance plus Jira (always shown when any Jira rows exist)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200
    html = r.text
    assert 'role="tablist"' in html or 'class="tab-nav"' in html
    assert 'data-tab="bigquery"' in html or 'id="tab-bigquery"' in html
    assert 'data-tab="keboola"' in html or 'id="tab-keboola"' in html


def test_admin_tables_active_tab_matches_instance_type(seeded_app, monkeypatch):
    """When data_source.type='bigquery', the BigQuery tab is the
    initially-active one. Operator can still switch to Keboola tab if
    they want to register a secondary source."""
    fake_cfg = {"data_source": {"type": "bigquery", "bigquery": {"project": "p"}}}
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg, raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.get("/admin/tables", headers=_auth(token))
        html = r.text
        # The BQ tab content is the visible one initially.
        # Either a class="active" on the BQ tab button, or aria-selected="true".
        assert (
            'data-tab="bigquery" class="tab active"' in html
            or 'data-tab="bigquery" aria-selected="true"' in html
        )
    finally:
        reset_cache()


def test_admin_tables_each_tab_has_register_button(seeded_app):
    """Each writable source tab has its own Register button. Jira is
    read-only (no Register)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    # Each Register button is scoped to its tab — id distinguishes.
    # We check presence of the registration trigger elements.
    assert 'id="bqRegisterBtn"' in html or 'data-register-source="bigquery"' in html
    assert 'id="kbRegisterBtn"' in html or 'data-register-source="keboola"' in html
    # No Jira register button (Jira is webhook-driven).
    assert 'data-register-source="jira"' not in html


def test_admin_tables_listing_per_tab(seeded_app):
    """The registry table is rendered per tab — each tab has its own
    <tbody> filtered by source_type. Listing JS reads tables from the
    catalog API and routes each row into the matching tab's <tbody>."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    assert 'id="bqTableListing"' in html
    assert 'id="kbTableListing"' in html
    assert 'id="jiraTableListing"' in html


def test_jira_tab_is_read_only(seeded_app):
    """Phase G: Jira tables are populated by webhooks, not by admin
    registration. Tab shows the listing + a hint pointing to docs;
    no Register button."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    jira_tab = html[html.index('id="tab-content-jira"'):]
    jira_tab = jira_tab[:jira_tab.index('</section>')]
    # No Register button.
    assert 'data-register-source="jira"' not in jira_tab
    assert 'jiraRegisterBtn' not in jira_tab
    # Hint pointing to docs (webhook-driven model).
    assert "webhook" in jira_tab.lower()
    # Listing div present.
    assert 'id="jiraTableListing"' in jira_tab


def test_admin_tables_tab_persists_in_url_hash(seeded_app):
    """Tab switching updates window.location.hash so refresh keeps the
    operator on the right tab. Verify the JS hooks for it are present."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    assert "location.hash" in html or "history.replaceState" in html
    # And initial-tab pickup from hash on load.
    assert "window.location.hash" in html or "getActiveTabFromHash" in html


def test_listing_partitions_rows_by_source_type(seeded_app):
    """When the operator has registered tables across all three sources,
    each tab's listing shows only the rows matching its source_type.
    JS-driven so we test by inspecting the JS branching logic indirectly:
    the renderer function takes a source filter and emits rows accordingly."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    c.post("/api/admin/register-table", headers=auth, json={
        "name": "kb_table", "source_type": "keboola", "bucket": "in.c-x",
        "source_table": "y", "query_mode": "local",
    })
    c.post("/api/admin/register-table", headers=auth, json={
        "name": "bq_table", "source_type": "bigquery",
        "query_mode": "materialized", "source_query": "SELECT 1",
    })

    r = c.get("/admin/tables", headers=auth)
    html = r.text
    # The renderer function is dispatched per tab. The test verifies the
    # JS code paths exist (we don't run JS in tests, just confirm the
    # template provides the wiring).
    assert "renderRegistryListing" in html or "loadRegistry" in html
    # Each tab listing div is the renderer target.
    assert "document.getElementById('bqTableListing')" in html
    assert "document.getElementById('kbTableListing')" in html
    assert "document.getElementById('jiraTableListing')" in html
