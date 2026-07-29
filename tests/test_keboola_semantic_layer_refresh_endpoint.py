"""End-to-end tests for POST /api/admin/run-keboola-semantic-layer-refresh."""

import asyncio
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """`_refresh_state` is a module-level dict — reset it around every test
    in this file so run order/leakage across tests can't affect assertions."""
    from app.api import keboola_semantic_layer_refresh as endpoint_module

    endpoint_module._refresh_state.update(
        {
            "run_id": None,
            "started_at": None,
            "last_completed_at": None,
            "last_status": None,
            "last_result": None,
        }
    )
    yield
    endpoint_module._refresh_state.update(
        {
            "run_id": None,
            "started_at": None,
            "last_completed_at": None,
            "last_status": None,
            "last_result": None,
        }
    )


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


def test_run_refresh_maps_returned_error_status_to_502(seeded_app):
    """sync_semantic_layer() reports config/upstream failures (missing
    credentials, Storage/Metastore API errors) by *returning*
    {"status": "error"} rather than raising — the endpoint must not treat
    that as a 200 success (previously: the admin UI showed a false "OK"
    after a failed sync)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    fake_result = {"status": "error", "error": "Keboola credentials not configured"}
    with patch("app.api.keboola_semantic_layer_refresh.sync_semantic_layer", return_value=fake_result):
        r = c.post(
            "/api/admin/run-keboola-semantic-layer-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 502
    assert r.json()["detail"] == "Keboola credentials not configured"


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


class TestLastRefreshSummary:
    """`get_last_refresh_summary()` — in-memory (since-last-restart) status
    the admin UI reads so a never-synced-yet or failed-last-attempt state is
    visible even when metric/glossary counts are currently zero (#953)."""

    def test_initial_state_is_never_synced(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import get_last_refresh_summary

        summary = get_last_refresh_summary()
        assert summary["last_completed_at"] is None
        assert summary["last_status"] is None
        assert summary["last_result"] is None

    def test_successful_refresh_records_summary(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import get_last_refresh_summary

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_result = {"status": "ok", "created_or_updated": 5, "pruned": 1}
        with patch("app.api.keboola_semantic_layer_refresh.sync_semantic_layer", return_value=fake_result):
            r = c.post(
                "/api/admin/run-keboola-semantic-layer-refresh",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, r.text

        summary = get_last_refresh_summary()
        assert summary["last_status"] == "ok"
        assert summary["last_completed_at"]
        assert summary["last_result"]["created_or_updated"] == 5
        # In-flight tracking still clears back to None once the run finishes.
        assert endpoint_module_state()["run_id"] is None
        assert endpoint_module_state()["started_at"] is None

    def test_master_token_error_records_failure_summary(self, seeded_app):
        from app.api.keboola_semantic_layer_refresh import get_last_refresh_summary
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

        summary = get_last_refresh_summary()
        assert summary["last_status"] == "error"
        assert summary["last_completed_at"]
        assert "master token" in summary["last_result"]

    def test_returned_error_status_records_failure_summary(self, seeded_app):
        """A returned {"status": "error"} dict (not an exception) must also
        flip last_status to "error" — this is the case that previously
        recorded "ok" unconditionally and showed a false-green summary."""
        from app.api.keboola_semantic_layer_refresh import get_last_refresh_summary

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_result = {"status": "error", "error": "Keboola credentials not configured"}
        with patch("app.api.keboola_semantic_layer_refresh.sync_semantic_layer", return_value=fake_result):
            r = c.post(
                "/api/admin/run-keboola-semantic-layer-refresh",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 502

        summary = get_last_refresh_summary()
        assert summary["last_status"] == "error"
        assert summary["last_completed_at"]
        assert summary["last_result"] == "Keboola credentials not configured"

    def test_unexpected_exception_also_records_failure_summary(self, seeded_app):
        """A non-MasterTokenRequiredError failure must still leave a visible
        trace in the summary, not just re-raise silently (#953)."""
        from app.api.keboola_semantic_layer_refresh import get_last_refresh_summary
        from fastapi.testclient import TestClient
        from app.main import create_app

        # A fresh, un-raising client: the default `seeded_app` TestClient
        # re-raises unhandled 500s in-test (raise_server_exceptions=True);
        # here we only care that the state got recorded before propagation.
        app = create_app()
        c = TestClient(app, raise_server_exceptions=False)
        token = seeded_app["admin_token"]
        with patch(
            "app.api.keboola_semantic_layer_refresh.sync_semantic_layer",
            side_effect=RuntimeError("boom"),
        ):
            r = c.post(
                "/api/admin/run-keboola-semantic-layer-refresh",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 500

        summary = get_last_refresh_summary()
        assert summary["last_status"] == "error"
        assert "boom" in summary["last_result"]

    def test_per_source_breakdown_flows_through_response(self, seeded_app):
        """sync_semantic_layer() now returns a top-level 'sources' list with
        per-source dicts (connection_id, name, status, counters, optional error).
        The endpoint must pass this through unchanged to the response and record
        it in the summary (regression guard: the dict passthrough at
        _record_completion and response return must not lose 'sources')."""
        from app.api.keboola_semantic_layer_refresh import get_last_refresh_summary

        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        fake_result = {
            "status": "ok",
            "created_or_updated": 5,
            "pruned": 1,
            "sources": [
                {
                    "connection_id": "conn_1",
                    "name": "S3 Source",
                    "status": "ok",
                    "created_or_updated": 3,
                    "pruned": 0,
                },
                {
                    "connection_id": "conn_2",
                    "name": "GCS Source",
                    "status": "ok",
                    "created_or_updated": 2,
                    "pruned": 1,
                },
            ],
        }
        with patch("app.api.keboola_semantic_layer_refresh.sync_semantic_layer", return_value=fake_result):
            r = c.post(
                "/api/admin/run-keboola-semantic-layer-refresh",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        # Verify response contains sources list
        assert "sources" in body
        assert len(body["sources"]) == 2
        assert body["sources"][0]["connection_id"] == "conn_1"
        assert body["sources"][1]["name"] == "GCS Source"

        # Verify summary also preserves sources
        summary = get_last_refresh_summary()
        assert summary["last_status"] == "ok"
        assert "sources" in summary["last_result"]
        assert len(summary["last_result"]["sources"]) == 2


def endpoint_module_state():
    from app.api import keboola_semantic_layer_refresh as endpoint_module

    return endpoint_module._refresh_state
