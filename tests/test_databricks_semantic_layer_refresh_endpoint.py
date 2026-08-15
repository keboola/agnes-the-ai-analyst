"""End-to-end tests for POST /api/admin/run-databricks-semantic-layer-refresh."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """`_refresh_state` is a module-level dict — reset it around every test."""
    from app.api import databricks_semantic_layer_refresh as endpoint_module

    blank = {
        "run_id": None,
        "started_at": None,
        "last_completed_at": None,
        "last_status": None,
        "last_result": None,
    }
    endpoint_module._refresh_state.update(blank)
    yield
    endpoint_module._refresh_state.update(blank)


def _post(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    return c.post(
        "/api/admin/run-databricks-semantic-layer-refresh",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_run_refresh_returns_sync_result(seeded_app):
    fake_result = {
        "status": "ok",
        "created_or_updated": 4,
        "pruned": 1,
        "metric_views_seen": 2,
        "skipped_unparseable": 0,
        "skipped_conflict": 0,
        "source_ref": "dbc-test.cloud.databricks.com",
    }
    with patch(
        "app.api.databricks_semantic_layer_refresh.sync_semantic_layer",
        return_value=fake_result,
    ):
        r = _post(seeded_app)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["created_or_updated"] == 4
    assert body["pruned"] == 1
    assert body["metric_views_seen"] == 2
    assert body["run_id"]
    assert body["started_at"]


def test_unconfigured_instance_answers_400(seeded_app):
    fake_result = {
        "status": "error",
        "error": "Databricks is not configured",
        "code": "credentials_not_configured",
    }
    with patch(
        "app.api.databricks_semantic_layer_refresh.sync_semantic_layer",
        return_value=fake_result,
    ):
        r = _post(seeded_app)
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_upstream_client_error_answers_400(seeded_app):
    fake_result = {"status": "error", "error": "permission denied", "code": "upstream_client_error"}
    with patch(
        "app.api.databricks_semantic_layer_refresh.sync_semantic_layer",
        return_value=fake_result,
    ):
        r = _post(seeded_app)
    assert r.status_code == 400


def test_upstream_error_answers_502(seeded_app):
    fake_result = {"status": "error", "error": "warehouse unreachable", "code": "upstream_error"}
    with patch(
        "app.api.databricks_semantic_layer_refresh.sync_semantic_layer",
        return_value=fake_result,
    ):
        r = _post(seeded_app)
    assert r.status_code == 502


def test_unmapped_error_code_stays_502(seeded_app):
    fake_result = {"status": "error", "error": "mystery", "code": "brand_new_code"}
    with patch(
        "app.api.databricks_semantic_layer_refresh.sync_semantic_layer",
        return_value=fake_result,
    ):
        r = _post(seeded_app)
    assert r.status_code == 502


def test_requires_admin(seeded_app):
    c = seeded_app["client"]
    r = c.post("/api/admin/run-databricks-semantic-layer-refresh")
    assert r.status_code in (401, 403)
