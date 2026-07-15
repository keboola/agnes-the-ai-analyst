"""Guard: the Tier-2 event-loop-offload handlers must stay synchronous, and
the upload handlers must keep routing their blocking file move through the
thread pool.

Plain ``def`` route handlers are auto-offloaded by FastAPI to the anyio
thread pool, so their synchronous DuckDB + filesystem I/O never blocks the
single uvicorn event loop. If one of these is reverted to ``async def`` it
would run on the loop again and one slow local/filesystem-I/O request would
stall every other request. Mirrors ``TestRegisterTableHandlerIsSync`` in
tests/test_admin_bq_register.py.
"""

import inspect
import io
import shutil

import pytest

from app.api import upload as upload_mod
from app.api.catalog import list_catalog_tables
from app.api.data import download_table
from app.api.marketplace import list_items
from app.api.memory import get_bundle
from app.api.upload import upload_artifact, upload_session
from app.web.router import home_page


def test_offloaded_handlers_are_sync():
    """These must be plain ``def`` so FastAPI runs them in the thread pool."""
    for handler in (
        list_catalog_tables,
        download_table,
        home_page,
        get_bundle,
        list_items,
    ):
        assert not inspect.iscoroutinefunction(handler), (
            f"{handler.__module__}.{handler.__name__} must be a sync ``def`` "
            "handler so it runs in the anyio thread pool, not on the event "
            "loop (see PR #188 Tier 1 / this Tier 2 rollout)."
        )


def test_upload_handlers_stay_async():
    """Upload handlers stream the request body (``await``), so they stay
    async; their blocking file move is offloaded via ``run_in_threadpool``."""
    for handler in (upload_session, upload_artifact):
        assert inspect.iscoroutinefunction(handler), (
            f"{handler.__module__}.{handler.__name__} streams the request "
            "body and must remain ``async def``."
        )


@pytest.mark.parametrize(
    "endpoint, filename, content_type",
    [
        ("/api/upload/sessions", "offload-guard.jsonl", "application/x-ndjson"),
        ("/api/upload/artifacts", "offload-guard.html", "text/html"),
    ],
)
def test_upload_move_is_offloaded_to_threadpool(
    seeded_app, analyst_user, monkeypatch, endpoint, filename, content_type
):
    """The blocking ``shutil.move`` must go through ``run_in_threadpool`` so a
    slow filesystem move never runs on the event loop.

    The ``async def`` guard above does not catch this: a revert to a bare
    ``shutil.move(...)`` keeps the handler async and still returns 200, so the
    existing upload success tests would pass while the event-loop stall is
    silently reintroduced. This spies on the offload wrapper and asserts the
    move was routed through it.
    """
    recorded: list = []
    real_run_in_threadpool = upload_mod.run_in_threadpool

    async def spy(func, *args, **kwargs):
        recorded.append(func)
        return await real_run_in_threadpool(func, *args, **kwargs)

    monkeypatch.setattr(upload_mod, "run_in_threadpool", spy)

    files = {"file": (filename, io.BytesIO(b"<h1>offload</h1>"), content_type)}
    resp = seeded_app["client"].post(endpoint, files=files, headers=analyst_user)
    assert resp.status_code == 200

    assert any(func is shutil.move for func in recorded), (
        f"{endpoint}: shutil.move was not routed through run_in_threadpool — "
        "the blocking file move would run on the event loop."
    )
