"""End-to-end tests for POST /api/admin/run-keboola-semantic-layer-refresh."""

import asyncio
from unittest.mock import patch


def test_run_refresh_returns_sync_result(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    fake_result = {
        "status": "ok",
        "created_or_updated": 3,
        "pruned": 0,
        "skipped_unresolved_table": 1,
        "skipped_foreign_alias": 0,
    }
    with patch("app.api.keboola_semantic_layer_refresh.sync_semantic_layer", return_value=fake_result):
        r = c.post(
            "/api/admin/run-keboola-semantic-layer-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["created_or_updated"] == 3
    assert body["pruned"] == 0
    assert body["skipped_unresolved_table"] == 1
    assert body["skipped_foreign_alias"] == 0
    assert body["run_id"]
    assert body["started_at"]


def test_run_refresh_maps_master_token_error_to_400(seeded_app):
    from connectors.keboola.semantic_layer import MasterTokenRequiredError

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    with patch(
        "app.api.keboola_semantic_layer_refresh.sync_semantic_layer",
        side_effect=MasterTokenRequiredError("needs a master token"),
    ):
        r = c.post(
            "/api/admin/run-keboola-semantic-layer-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 400
    assert "master token" in r.json()["detail"]


def test_run_refresh_requires_admin(seeded_app):
    c = seeded_app["client"]
    r = c.post("/api/admin/run-keboola-semantic-layer-refresh")
    assert r.status_code == 401


def test_run_refresh_returns_409_when_already_running(seeded_app):
    from app.api import keboola_semantic_layer_refresh as endpoint_module

    async def _acquire():
        await endpoint_module._refresh_lock.acquire()

    asyncio.run(_acquire())
    try:
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        r = c.post(
            "/api/admin/run-keboola-semantic-layer-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "already_running"
    finally:
        endpoint_module._refresh_lock.release()
