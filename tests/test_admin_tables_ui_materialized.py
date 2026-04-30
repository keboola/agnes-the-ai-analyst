"""`/admin/tables` register modal exposes the materialized BQ controls.

The backend supports `query_mode='materialized'` since v0.25.0 (PR #148).
The Jinja template at `app/web/templates/admin_tables.html` was updated to
add a `Mode` dropdown plus a `source_query` textarea so operators can
register materialized BQ tables from the UI without reaching for the CLI.

This test is structural-only (no headless browser): it loads the template
through the running app and asserts the expected element ids + attributes
are present in the rendered HTML for a `data_source_type='bigquery'`
deployment.
"""
import pytest
from unittest.mock import MagicMock


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


def test_admin_tables_renders_bq_mode_selector(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    html = r.text

    # Mode dropdown with both options.
    assert 'id="bqQueryMode"' in html
    assert 'value="remote"' in html and 'value="materialized"' in html
    assert "onBqModeChange" in html

    # Materialized-only field (textarea).
    assert 'id="bqSourceQuery"' in html
    # Visibility class so the JS toggle can show/hide it as a group.
    assert "bq-mode-materialized" in html

    # Remote-mode fields kept under their own visibility class.
    assert 'id="bqDataset"' in html
    assert 'id="bqSourceTable"' in html
    assert "bq-mode-remote" in html


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
        assert 'id="bqQueryMode"' not in html
        assert 'id="bqSourceQuery"' not in html
        # Keboola form's regBucket / regTableId still there.
        assert 'id="regTableId"' in html
        assert 'id="regBucket"' in html
    finally:
        reset_cache()
