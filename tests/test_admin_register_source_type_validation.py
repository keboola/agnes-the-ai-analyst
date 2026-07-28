"""POST /api/admin/register-table validates that the requested source_type
is actually configured on this instance — otherwise the row would never
sync (no Keboola URL/token to ATTACH against, or no BigQuery project).

E2E sub-agent finding 2026-05-01: instance configured with
`data_source.type='bigquery'` and no `data_source.keboola.*` block. Admin
POSTs `{source_type: 'keboola'}` → returns 201, row lands in registry but
never syncs. No upfront validation surfaces the misconfig.
"""
from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bq_only_instance(monkeypatch):
    """Instance configured ONLY for BigQuery — no keboola block."""
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {"project": "my-test-project", "location": "us"},
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config", lambda: fake_cfg, raising=False,
    )
    monkeypatch.setattr(
        "config.loader.load_instance_config", lambda: fake_cfg, raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    yield fake_cfg
    reset_cache()


@pytest.fixture
def keboola_only_instance(monkeypatch):
    """Instance configured ONLY for Keboola — no bigquery block."""
    fake_cfg = {
        "data_source": {
            "type": "keboola",
            "keboola": {
                "stack_url": "https://connection.keboola.com",
                "project_id": "1234",
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config", lambda: fake_cfg, raising=False,
    )
    monkeypatch.setattr(
        "config.loader.load_instance_config", lambda: fake_cfg, raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    yield fake_cfg
    reset_cache()


def test_register_keboola_on_bq_only_instance_rejected(seeded_app, bq_only_instance):
    """source_type='keboola' against a BQ-only instance must 422 with a
    message pointing the operator at /admin/server-config to enable the
    secondary source. Without this, the row lands in the registry and
    never syncs because there's no Keboola URL/token to ATTACH."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "should_reject",
            "source_type": "keboola",
            "bucket": "in.c-main",
            "source_table": "events",
            "query_mode": "local",
        },
        headers=_auth(token),
    )
    assert r.status_code == 422, r.json()
    detail = str(r.json().get("detail", "")).lower()
    assert "not configured" in detail or "not enabled" in detail
    assert "keboola" in detail
    assert "bigquery" in detail  # message names the actually-configured source


def test_register_bq_on_keboola_only_instance_rejected(seeded_app, keboola_only_instance):
    """Symmetric: source_type='bigquery' on a Keboola-only instance
    rejects."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "should_reject_bq",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "query_mode": "remote",
        },
        headers=_auth(token),
    )
    assert r.status_code == 422, r.json()
    detail = str(r.json().get("detail", "")).lower()
    assert "not configured" in detail
    assert "bigquery" in detail


def test_register_matching_source_type_succeeds(seeded_app, bq_only_instance):
    """Sanity: BQ row on a BQ instance still works — the new validation
    only refuses MISmatches."""
    from unittest.mock import MagicMock
    import pytest as _pt

    # Stub the BQ rebuild to keep test offline.
    from connectors.bigquery import extractor as _bq
    _orig = _bq.rebuild_from_registry
    _bq.rebuild_from_registry = MagicMock(return_value={
        "project_id": "my-test-project", "tables_registered": 1,
        "errors": [], "skipped": False,
    })
    from src import orchestrator as _orch
    _orig_orch = _orch.SyncOrchestrator
    _orch.SyncOrchestrator = lambda *a, **kw: MagicMock()
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.post(
            "/api/admin/register-table",
            json={
                "name": "matching_bq",
                "source_type": "bigquery",
                "bucket": "analytics",
                "source_table": "orders",
                "query_mode": "remote",
            },
            headers=_auth(token),
        )
        # 200 (sync materialize) or 202 (timeout). Both are success codes here.
        assert r.status_code in (200, 202), r.json()
    finally:
        _bq.rebuild_from_registry = _orig
        _orch.SyncOrchestrator = _orig_orch


def test_register_jira_does_not_require_data_source_match(seeded_app, bq_only_instance):
    """Jira rows are ingested via webhooks, not via `data_source.*` config.
    They should be allowed regardless of the configured `data_source.type`
    so a BQ-primary instance can still receive Jira webhooks."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "jira_issues",
            "source_type": "jira",
            "query_mode": "local",
        },
        headers=_auth(token),
    )
    # Jira goes through the insert-only branch and returns 201.
    assert r.status_code == 201, r.json()


def test_register_omitted_source_type_passes_through(seeded_app, bq_only_instance):
    """Backwards compat: callers that don't set source_type (legacy CLI
    scripts) must still succeed — the route resolves source_type later
    against `get_data_source_type()`. The new validator only refuses
    EXPLICIT mismatches."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "legacy_caller",
            # source_type omitted entirely
            "query_mode": "local",
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
