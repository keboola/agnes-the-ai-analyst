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
