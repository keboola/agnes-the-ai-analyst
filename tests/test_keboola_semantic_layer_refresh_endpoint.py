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


def test_non_master_token_answers_400_however_many_connections_exist(seeded_app):
    """The same misconfiguration must not change status with the topology.

    The single-source paths let MasterTokenRequiredError propagate and the
    endpoint maps it to 400 (test above). The multi-source loop CAPTURES it
    per connection, so without a code the aggregate carried none and fell
    back to 502 — the same broken token answering 400 on one instance and
    502 on another, which is exactly the inconsistency this endpoint's status
    mapping exists to remove. Devin Review on #1242.
    """
    from connectors.keboola.semantic_layer import MasterTokenRequiredError

    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Drive the REAL master-source loop — patching only its two edges, so the
    # code the loop attaches (or fails to attach) is what decides the status.
    # Hard-coding the aggregate here would have tested the mapping table and
    # nothing else, and passed just as happily with the bug in place.
    fake_source = {
        "connection_id": "conn-a",
        "name": "Production",
        "stack_url": "https://connection.keboola.com",
        "token": "downgraded-token",
        "project_id": None,
        "project_name": "",
    }
    with (
        patch("connectors.keboola.semantic_layer._enumerate_master_sources", return_value=[fake_source]),
        patch(
            "connectors.keboola.semantic_layer._sync_one_source",
            side_effect=MasterTokenRequiredError("needs a master token"),
        ),
    ):
        r = c.post(
            "/api/admin/run-keboola-semantic-layer-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 400, r.text
    assert "master token" in r.json()["detail"]


def test_run_refresh_maps_returned_error_to_an_error_status(seeded_app):
    """sync_semantic_layer() reports config/upstream failures (missing
    credentials, Storage/Metastore API errors) by *returning*
    {"status": "error"} rather than raising — the endpoint must not treat
    that as a 200 success (previously: the admin UI showed a false "OK"
    after a failed sync).

    A result with no ``code`` keeps the historical 502 so an older/unknown
    error shape never silently becomes a 4xx."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    fake_result = {"status": "error", "error": "Keboola semantic layer sync failed"}
    with patch("app.api.keboola_semantic_layer_refresh.sync_semantic_layer", return_value=fake_result):
        r = c.post(
            "/api/admin/run-keboola-semantic-layer-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 502
    assert r.json()["detail"] == "Keboola semantic layer sync failed"


@pytest.mark.parametrize(
    "code,expected_status",
    [
        # The admin can fix these, so 502 Bad Gateway is a lie that reads as an
        # Agnes outage — the misdiagnosis this mapping exists to prevent.
        ("credentials_not_configured", 400),
        ("upstream_client_error", 400),
        # A genuine outage is still a gateway failure.
        ("upstream_error", 502),
        ("something_new", 502),
    ],
)
def test_run_refresh_status_follows_the_error_code(seeded_app, code, expected_status):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    fake_result = {"status": "error", "error": "nope", "code": code}
    with patch("app.api.keboola_semantic_layer_refresh.sync_semantic_layer", return_value=fake_result):
        r = c.post(
            "/api/admin/run-keboola-semantic-layer-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == expected_status
    assert r.json()["detail"] == "nope"


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
        fake_result = {
            "status": "error",
            "error": "Keboola credentials not configured",
            "code": "credentials_not_configured",
        }
        with patch("app.api.keboola_semantic_layer_refresh.sync_semantic_layer", return_value=fake_result):
            r = c.post(
                "/api/admin/run-keboola-semantic-layer-refresh",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400

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


def test_coverage_endpoint_returns_computed_sources(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    fake = {
        "sources": [
            {
                "connection_id": "conn-1",
                "name": "Demo project",
                "metrics": {"upstream": 50, "importable": 0},
                "glossary": {"upstream": 9},
                "unregistered_tables": ["in.c-demo.subscriptions"],
                "blocked": [],
                "warnings": [{"code": "no_metrics_bound", "message": "…"}],
            }
        ]
    }
    with patch(
        "connectors.keboola.semantic_layer.compute_semantic_coverage",
        return_value=fake,
    ):
        r = c.get(
            "/api/admin/semantic-layer/coverage",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    source = r.json()["sources"][0]
    assert source["metrics"] == {"upstream": 50, "importable": 0}
    assert source["unregistered_tables"] == ["in.c-demo.subscriptions"]
    assert [w["code"] for w in source["warnings"]] == ["no_metrics_bound"]


def test_coverage_endpoint_requires_admin(seeded_app):
    """Coverage names upstream project ids and dataset paths — admin-only, like
    every other surface over a connection's configuration."""
    c = seeded_app["client"]
    r = c.get("/api/admin/semantic-layer/coverage")
    assert r.status_code in (401, 403), r.text


class TestBackgroundRefreshSingleFlight:
    """The login-provisioning tail shares the endpoint's single-flight guard;
    a concurrent second caller must SKIP, never queue a duplicate run — and
    the claim must clear even when the sync raises."""

    def test_second_concurrent_caller_skips(self, monkeypatch):
        import asyncio
        import threading

        import app.api.keboola_semantic_layer_refresh as kslr

        calls = []
        release = threading.Event()

        def fake_sync():
            calls.append(1)
            # Hold the run in flight until the test releases it. An
            # instantly-returning fake would NOT guarantee overlap: on
            # Python 3.13 the executor future can resolve before the
            # awaiting task ever yields, so two gather()ed callers run
            # strictly one after the other — and a second refresh AFTER a
            # completed one is correct behavior, not the duplicate this
            # test exists to catch.
            release.wait(timeout=10)
            return {"status": "ok"}

        monkeypatch.setattr(kslr, "sync_semantic_layer", fake_sync)

        async def run_overlapped():
            first = asyncio.ensure_future(kslr.run_semantic_layer_refresh_background(trigger="login-a"))
            try:
                for _ in range(1000):
                    if calls:
                        break
                    await asyncio.sleep(0.005)
                assert calls, "the first caller never entered the sync"
                # The overlapping caller must return immediately (skip) —
                # one that queued behind the in-flight run would still be
                # waiting when this times out.
                await asyncio.wait_for(kslr.run_semantic_layer_refresh_background(trigger="login-b"), timeout=5)
                assert not first.done()
            finally:
                release.set()
            await first

        asyncio.run(run_overlapped())
        assert len(calls) == 1

    def test_claim_clears_after_a_failed_run(self, monkeypatch):
        import asyncio

        import app.api.keboola_semantic_layer_refresh as kslr

        calls = []

        def flaky_sync():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("upstream exploded")
            return {"status": "ok"}

        monkeypatch.setattr(kslr, "sync_semantic_layer", flaky_sync)
        asyncio.run(kslr.run_semantic_layer_refresh_background(trigger="first"))
        asyncio.run(kslr.run_semantic_layer_refresh_background(trigger="second"))
        assert len(calls) == 2
        assert kslr._refresh_claimed is False
