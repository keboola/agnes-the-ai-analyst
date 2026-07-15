"""Guard: the Tier-2 event-loop-offload handlers must stay synchronous.

Plain ``def`` route handlers are auto-offloaded by FastAPI to the anyio
thread pool, so their synchronous DuckDB + filesystem I/O never blocks the
single uvicorn event loop. If one of these is reverted to ``async def`` it
would run on the loop again and one slow local/filesystem-I/O request would
stall every other request. Mirrors ``TestRegisterTableHandlerIsSync`` in
tests/test_admin_bq_register.py.
"""

import inspect

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
