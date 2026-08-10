"""UI tests for the package-centric admin/tables layout.

Pre-rewrite this file asserted the per-connector tab nav (BigQuery /
Keboola / Jira / Agnes internal) drove the page layout. The
package-centric rewrite ('data packages handled on the side / weird /
everything must live within a group in data packages' user feedback)
dropped the tab nav in favour of:
  - top action bar with `+ Register new table ▾` dropdown
  - one `<details>` per Data Package with member tables inline
  - 'Unpackaged tables (N — needs packaging)' yellow callout
Register / edit modals stay in DOM (the file name keeps `_tab_ui` for
git history continuity).
"""
import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_admin_tables_renders_action_bar(seeded_app):
    """Top-of-page action bar replaces the prior tab nav. Carries the
    Register-new-table dropdown + the Data Package action set."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200
    html = r.text
    assert 'id="adminTablesActionBar"' in html
    assert 'id="registerNewTableBtn"' in html
    assert 'id="registerNewTableMenu"' in html
    # Connector-typed Register entry points exist as dropdown items —
    # the page is package-centric but the operator still needs to pick
    # which register modal to open.
    assert 'data-register-source="bigquery"' in html
    assert 'data-register-source="keboola"' in html
    # Jira is webhook-driven — no Register button at all (the dropdown
    # surfaces a 'see docs' link instead).
    assert 'onclick="closeRegisterNewTableMenu(); openRegisterModal(\'jira\')"' not in html


def test_admin_tables_active_register_modal_matches_instance_type(seeded_app, monkeypatch):
    """The instance's data_source.type still drives the body
    `data-source-type` marker, which the JS uses as a default when
    `openRegisterModal()` is called without an explicit source."""
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
        # body carries the data-source-type marker → DATA_SOURCE_TYPE
        # picks it up so openRegisterModal() (no arg) routes to BQ.
        assert 'data-source-type="bigquery"' in html
    finally:
        reset_cache()


def test_admin_tables_register_dropdown_lists_connectors(seeded_app):
    """The `+ Register new table ▾` dropdown lists the connectors that
    have a register flow (BigQuery + Keboola). Jira is webhook-only —
    appears as a docs link, not a register trigger."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    # The two register entry points are present (in the dropdown).
    assert "openRegisterModal('bigquery')" in html
    assert "openRegisterModal('keboola')" in html
    # Jira's read-only nature is communicated via a docs link.
    assert "docs/connectors/jira.md" in html


def test_admin_tables_layout_renders_packages_and_unpackaged_hosts(seeded_app):
    """The package-centric layout has two top-level hosts hydrated by
    loadAdminTablesLayout(): one for Data Packages, one for Unpackaged
    tables. Both render client-side from the unified /api/admin/registry
    + /api/admin/data-packages calls."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    assert 'id="adminTablesLayout"' in html
    assert 'id="adminTablesLayoutPackages"' in html
    assert 'id="adminTablesLayoutUnpackaged"' in html
    # The hydrator function exists.
    assert "function loadAdminTablesLayout" in html
    # Pre-rewrite loadDataPackagesSection is aliased for backward compat —
    # the Create / Edit / Delete-package modal refresh hooks still call it.
    assert "loadDataPackagesSection" in html


def test_admin_tables_no_connector_tab_nav(seeded_app):
    """Connector tab nav was dropped — every table now appears under a
    Data Package or in 'Unpackaged tables'. Verify the prior tab markers
    are gone."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    # No tab nav structural markers.
    assert 'class="tab-nav"' not in html
    assert 'role="tablist"' not in html
    assert 'id="tab-content-bigquery"' not in html
    assert 'id="tab-content-keboola"' not in html
    assert 'id="tab-content-jira"' not in html
    assert 'id="tab-content-internal"' not in html
    # No per-tab listing divs either (rolled into loadAdminTablesLayout).
    assert 'id="bqTableListing"' not in html
    assert 'id="kbTableListing"' not in html
    assert 'id="jiraTableListing"' not in html
    assert 'id="internalTableListing"' not in html


def test_admin_tables_renders_register_modals_in_dom(seeded_app):
    """Register / edit modals stay in DOM after the tab nav drop — the
    `+ Register new table ▾` dropdown items open them by id. Tests for
    the modal's form fields live in test_admin_tables_ui_materialized."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    html = r.text
    assert 'id="registerBqModal"' in html
    assert 'id="editBqModal"' in html
    assert 'id="registerKeboolaModal"' in html
    assert 'id="editKeboolaModal"' in html


def test_registry_listing_renders_manage_access_button(seeded_app):
    """Each row in the package-centric listing has an Edit affordance.
    The Manage-access deep-link helper survives in the JS (used by other
    surfaces — kept callable). It now targets /admin/groups: grants are
    group-scoped, so the operator picks a group and the hash rides along
    to that group's Access tab."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Register a table so the API will surface at least one row.
    c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "test_orders",
            "source_type": "keboola",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "query_mode": "local",
        },
    )

    r = c.get("/admin/tables", headers=auth)
    body = r.text
    # The manageAccess() helper still exists in the JS (deep-links to the
    # group list scoped to a table_id).
    assert "function manageAccess(" in body or "manageAccess =" in body
    # It targets the group list, which is where a grant target is chosen.
    assert "/admin/groups#table:" in body


def test_group_list_forwards_the_table_deep_link(seeded_app):
    """/admin/groups reads `#table:<id>` on load: it shows the "pick a
    group" banner and carries the hash onto each group's detail link."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/groups", headers={"Authorization": f"Bearer {token}"})
    body = r.text
    assert "location.hash" in body and "#table:" in body, \
        "/admin/groups must read the deep-link from URL on load"
    assert "gp-picking" in body, "the pick-a-group banner must be present"


def test_group_detail_applies_the_table_deep_link(seeded_app):
    """The group detail page's Access tab reads `#table:<id>` and pins its
    resource filter to that table — the far end of the deep link that used
    to terminate on the standalone /admin/access page."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    groups = c.get("/api/admin/groups", headers=auth).json()
    assert groups, "seeded instance must have at least the system groups"

    r = c.get(f"/admin/groups/{groups[0]['id']}", headers=auth)
    body = r.text
    assert r.status_code == 200
    assert "location.hash" in body and "#table:" in body
    # The grant matrix itself now lives here.
    assert "/api/admin/access-overview" in body
    assert 'data-pane="access"' in body
