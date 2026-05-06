"""Process-wide singleton guard around POST /api/sync/trigger.

Without it, two near-simultaneous trigger calls each launch their own
extractor subprocess, both write `extract.duckdb`, fight for the file
lock, starve uvicorn, and Docker flips the container to `unhealthy`.

These tests cover the trigger handler's 409 fast-fail (the
operator-visible behavior) and the in-`_run_sync` defense-in-depth
(if something bypasses the handler).
"""
from unittest.mock import patch

import pytest

from app.api import sync as sync_module


@pytest.fixture(autouse=True)
def reset_sync_lock():
    """Make sure each test starts with a free lock — and never leaves one
    held even if an assertion fires mid-test."""
    if sync_module._sync_lock.locked():
        sync_module._sync_lock.release()
    yield
    if sync_module._sync_lock.locked():
        sync_module._sync_lock.release()


def test_run_sync_skips_when_lock_held(capsys):
    """When `_sync_lock` is already held, `_run_sync` no-ops with a log
    line instead of starting a second extractor subprocess."""
    sync_module._sync_lock.acquire()

    # Patch the heavy parts so a successful path would otherwise execute.
    # Reaching any of them while the lock is held would be the bug.
    with patch("app.api.sync.subprocess.run") as run_mock, \
         patch("app.instance_config.get_data_source_type") as src_mock:
        sync_module._run_sync(tables=None)

    assert not run_mock.called, "extractor subprocess must not run when lock is held"
    assert not src_mock.called, "_run_sync must short-circuit before reading config"

    captured = capsys.readouterr()
    assert "another sync is already in flight" in captured.err


def test_run_sync_releases_lock_on_exception():
    """Even if the body throws, the lock must release so the next sync can
    run. Asserts the `finally:` covers all exit paths.

    `_run_sync` imports `get_data_source_type` lazily inside the body, so
    we patch the source module rather than the re-export in `app.api.sync`.
    """
    with patch(
        "app.instance_config.get_data_source_type",
        side_effect=RuntimeError("boom"),
    ):
        # Should not raise — `_run_sync` catches and logs
        sync_module._run_sync(tables=None)

    assert not sync_module._sync_lock.locked()


def test_trigger_endpoint_returns_409_when_locked():
    """Handler-level fast-fail: when a sync is already running, the
    trigger endpoint returns 409 without scheduling a second background
    task."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    # Stand up a minimal app exposing only the sync router. Bypass auth
    # by overriding the require_admin dependency.
    from app.auth.access import require_admin

    app = FastAPI()
    app.include_router(sync_module.router)
    app.dependency_overrides[require_admin] = lambda: {"id": "test", "email": "t@e"}
    client = TestClient(app)

    sync_module._sync_lock.acquire()
    try:
        resp = client.post("/api/sync/trigger")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "sync_already_in_progress"
    finally:
        sync_module._sync_lock.release()


def test_trigger_endpoint_succeeds_when_lock_free():
    """When the lock is free, the trigger endpoint schedules the
    background task and returns 200. The background task itself doesn't
    execute synchronously in TestClient — that's how FastAPI background
    tasks work — so we patch `_run_sync` to a no-op and only assert the
    handler shape."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.auth.access import require_admin

    app = FastAPI()
    app.include_router(sync_module.router)
    app.dependency_overrides[require_admin] = lambda: {"id": "test", "email": "t@e"}
    client = TestClient(app)

    with patch("app.api.sync._run_sync") as run_mock:
        resp = client.post("/api/sync/trigger")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "triggered"
        # BackgroundTasks runs after response; TestClient awaits them
        run_mock.assert_called_once()
