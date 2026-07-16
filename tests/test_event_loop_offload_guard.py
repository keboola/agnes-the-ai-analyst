"""Event-loop offload guard — pins the Tier 1 async→plain-def conversions.

Agnes runs as a single-process, single-event-loop uvicorn server. FastAPI runs
a plain ``def`` route handler / dependency in the anyio thread pool, but an
``async def`` one runs directly on the event loop. The auth/RBAC dependencies
below execute SYNCHRONOUS, blocking system-DB reads (on Postgres via a sync
SQLAlchemy engine + psycopg3). They run on nearly every request, so if any were
``async def`` a single slow read would freeze the whole process — the reverse
proxy cuts the request → 503 "system unavailable".

This ratchet asserts each stays a plain ``def`` (``inspect.iscoroutinefunction``
is False), so a future edit that reintroduces ``async def`` fails loudly here
instead of silently regressing latency in production. See PR #188's Tier 1
event-loop unblocking rollout for the convention.
"""

from __future__ import annotations

import inspect

import pytest

from app.api.broker import require_broker_ticket
from app.api.catalog import (
    get_metric,
    get_table_profile,
    list_catalog_tables,
    refresh_profile,
)
from app.api.claude_md import (
    admin_get_workspace_template,
    admin_preview_workspace_template,
    admin_put_workspace_template,
    admin_reset_workspace_template,
    get_welcome,
)
from app.api.sync import (
    get_sync_settings,
    get_table_subscriptions,
    pull_confirm,
    sync_manifest,
    sync_status,
    trigger_sync,
    update_sync_settings,
    update_table_subscriptions,
)
from app.auth.access import require_admin, require_resource_access
from app.auth.dependencies import (
    get_current_user,
    get_optional_user,
    require_session_token,
)
from app.resource_types import ResourceType


def test_get_current_user_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(get_current_user)


def test_get_optional_user_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(get_optional_user)


def test_require_session_token_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(require_session_token)


def test_require_admin_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(require_admin)


def test_require_broker_ticket_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(require_broker_ticket)


def test_require_resource_access_inner_dep_is_not_a_coroutine_function():
    # The factory itself is a plain def; the dependency FastAPI actually
    # resolves per request is the returned inner ``dep`` — that is the one
    # that must be offloaded to the thread pool.
    dep = require_resource_access(ResourceType.TABLE, "{table_id}")
    assert not inspect.iscoroutinefunction(dep)


# Route handlers in app/api/catalog.py, app/api/claude_md.py and app/api/sync.py
# that PR #890 converted from ``async def`` to plain ``def``. None of those three
# modules contains a single ``await`` — every handler does purely blocking
# synchronous DB/FS work — so each must stay a plain ``def`` to keep FastAPI
# offloading it to the thread pool. A revert to ``async def`` would put the
# blocking work back on the single event loop and stall concurrent requests.
_OFFLOADED_API_HANDLERS = [
    get_table_profile,
    list_catalog_tables,
    get_metric,
    refresh_profile,
    get_welcome,
    admin_get_workspace_template,
    admin_put_workspace_template,
    admin_reset_workspace_template,
    admin_preview_workspace_template,
    sync_manifest,
    pull_confirm,
    sync_status,
    trigger_sync,
    get_sync_settings,
    update_sync_settings,
    get_table_subscriptions,
    update_table_subscriptions,
]


@pytest.mark.parametrize("handler", _OFFLOADED_API_HANDLERS, ids=lambda h: h.__name__)
def test_api_route_handler_is_not_a_coroutine_function(handler):
    assert not inspect.iscoroutinefunction(handler)
