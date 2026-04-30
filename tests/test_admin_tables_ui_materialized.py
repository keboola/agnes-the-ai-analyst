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


def test_admin_tables_renders_bq_type_selector(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    html = r.text

    # Type selector with three operator-facing options. Backend payload
    # maps these onto query_mode (table+view → remote, query → materialized).
    assert 'id="bqEntityType"' in html
    assert 'value="table"' in html
    assert 'value="view"' in html
    assert 'value="query"' in html
    assert "onBqTypeChange" in html

    # Custom-SQL field + the "Use table as base" prefill button.
    assert 'id="bqSourceQuery"' in html
    assert "prefillFromTable" in html
    assert "bq-type-query" in html

    # Table/View shared inputs.
    assert 'id="bqDataset"' in html
    assert 'id="bqSourceTable"' in html
    assert "bq-type-table" in html
    assert "bq-type-view" in html

    # Vendor-agnostic — no internal issue refs in operator-facing UI text.
    assert "Milestone 2" not in html
    assert "issue #108" not in html


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
