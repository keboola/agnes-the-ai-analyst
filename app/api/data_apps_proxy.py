"""Auth-gated ingress proxy for hosted data apps (Task 8 of the v96 plan).

Composes the ``data_apps`` registry (``src/repositories/data_apps.py``),
the runner sidecar client (``src/data_apps/runner_client.py``), the
control-plane's RBAC predicate and shared deploy pipeline
(``app/api/data_apps.py``), and cross-process coordination
(``app/coordination``) into the public-facing surface end users actually
hit: ``https://<host>/apps/<slug>/...`` (or, in subdomain mode,
``https://<slug>.<subdomain_base>/...`` rewritten by
``app/data_apps_subdomain.py`` before it ever reaches routing).

Routes:

  - ``GET /apps/{slug}``                — redirect to the trailing-slash form
  - ``* /apps/{slug}/{path:path}``      — the proxy/wake/holding-page handler
  - ``WEBSOCKET /apps/{slug}/{path:path}`` — WS bridge to the app container

Per-request flow for the HTTP handler, after resolving the ``data_apps``
row and checking RBAC (``_can_view`` — owner, Admin, or a group grant):

  1. ``_touch`` — debounced ``last_request_at`` bump (coordination KV,
     30s TTL) so a bursty session doesn't hammer the registry with writes.
  2. Branch on ``row["state"]``:

     - ``running``    — stream-proxy to ``http://agnes-dataapp-<slug>:8888/<path>``.
       A connect failure here means the container is gone despite the
       registry believing it's up — flip to ``error`` and 502, rather than
       silently retrying or hanging.
     - ``sleeping``    — fire :func:`_trigger_wake` (idempotent via a
       coordination lease) and answer with the holding page / 503 JSON.
     - ``deploying``   — already waking (this request or a concurrent one
       already holds the wake lease) — same holding page, no second trigger.
     - ``stopped``/``created`` — a stopped app is an operator decision, not
       something a random inbound request should resurrect: 409, no wake.
     - ``error``       — 409 surfacing ``state_detail``.

Wake completion is NOT polled by this module — ``GET
/api/data-apps/{slug}/readiness`` (``app/api/data_apps.py``) flips
``deploying`` -> ``running`` itself once the runner reports ``ready``; the
holding page's own JS polls that endpoint. Two places document this same
fact on purpose (this module's docstring and the readiness endpoint's) so
neither can be edited without a reader noticing the other half of the
contract.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.data_apps import OwnerNotFoundError, _can_view, _feature_gate, redeploy_current
from app.auth.dependencies import _get_db, get_current_user
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination
from app.coordination.leases import default_holder_id
from src.data_apps.runner_client import RunnerClient, RunnerError, RunnerUnavailable
from src.repositories import data_apps_repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["data-apps-proxy"])

# Hop-by-hop headers (RFC 7230 §6.1) plus `host` — stripped in BOTH
# directions. `host` specifically must not ride through to the upstream
# (it would carry the caller's original Host, not `agnes-dataapp-<slug>`)
# nor back to the caller (httpx's own request to the upstream sets its own).
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}

# Caller credentials — REQUEST-direction only, never forwarded to the data
# app's own container. `Authorization` carries the caller's Agnes session/PAT
# (meant to authenticate to Agnes, not to the data app) and `Cookie` carries
# the `access_token` session cookie (same reasoning, plus it may now carry
# `Domain=.<parent>` in subdomain mode — see `session_cookie_domain()` — so
# it would otherwise ride straight into a container we don't control).
# Deliberately a separate set from `_HOP_BY_HOP` (different RFC, different
# rationale: hop-by-hop is a *protocol* concept, this is *security*) even
# though both end up filtered the same way on the request side. Response
# headers are never checked against this set — a data app setting its OWN
# `Set-Cookie` on the way back is legitimate and none of this proxy's business.
_CREDENTIAL_HEADERS = {"authorization", "cookie"}

_WAKE_LEASE_TTL_S = 120
_TOUCH_DEBOUNCE_TTL_S = 30


def _runner() -> RunnerClient:
    """Module-level indirection — the seam ``fake_runner``/``dead_runner``
    test fixtures monkeypatch. A SEPARATE seam from
    ``app.api.data_apps._runner`` (that one backs ``redeploy_current``'s
    ``up()`` call) — this one backs the direct ``resume()``/``status()``
    calls this module makes itself. Tests that need both call sites
    observed patch both module-level symbols to the same stub instance.
    """
    return RunnerClient()


def _upstream_client() -> httpx.AsyncClient:
    """Test seam: monkeypatch this to point at an
    ``httpx.MockTransport``-backed client instead of a real socket."""
    return httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=300, write=60, pool=5))


def _wants_json(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


def _get_row_or_404(slug: str) -> dict:
    row = data_apps_repo().get_by_slug(slug)
    if not row:
        raise HTTPException(status_code=404, detail="data_app_not_found")
    return row


def _touch(app_row: dict) -> None:
    """Debounced ``last_request_at`` bump — see module docstring step 1.

    Falls back to an un-debounced direct write when the coordination
    backend is unavailable (single-process dev semantics: a missing
    coordination backend must not silently stop idle-tracking from
    working at all, just lose the debounce).
    """
    key = f"dataapp:touch:{app_row['slug']}"
    try:
        if coordination().kv_get(key) is None:
            coordination().kv_set(key, "1", ttl_s=_TOUCH_DEBOUNCE_TTL_S)
            data_apps_repo().touch_last_request(app_row["id"])
    except CoordinationUnavailable:
        data_apps_repo().touch_last_request(app_row["id"])


async def _run_wake_fn(fn, row: dict) -> None:
    """Run ``fn(row)`` (a blocking sync callable — ``redeploy_current`` in
    production, a test double in ``tests/test_data_apps_proxy.py``) off
    the event loop via ``run_in_threadpool``, mapping ANY failure —
    including one raised from inside the backgrounded task itself, since
    nothing else observes it — onto ``set_state(row_id, "error", detail)``
    so a wake attempt never leaves the row wedged in ``deploying`` forever.

    Shared by ``_spawn_wake``'s production background task and its test
    replacement (``tests/test_data_apps_proxy.py``'s inline-await fixture)
    so the error-handling contract can't drift between the two.
    """
    repo = data_apps_repo()
    try:
        await run_in_threadpool(fn, row)
    except OwnerNotFoundError:
        repo.set_state(row["id"], "error", "owner_not_found")
    except (RunnerUnavailable, RunnerError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        repo.set_state(row["id"], "error", detail)
    except Exception as exc:  # noqa: BLE001 — must never leave the app wedged in "deploying"
        logger.exception("wake redeploy failed for data app %s", row.get("slug"))
        repo.set_state(row["id"], "error", str(exc))


async def _spawn_wake(fn, row: dict) -> None:
    """Production seam: schedule ``_run_wake_fn(fn, row)`` as a background
    ``asyncio.Task`` and return immediately — awaiting this coroutine costs
    ~nothing (it only awaits the trivial act of scheduling the task, never
    the task itself), so the request handler's wake trigger doesn't block
    on however long ``fn`` (e.g. ``redeploy_current``'s container
    pull/start) actually takes; the holding page is what the caller waits
    on instead, polling ``/readiness`` until the background task's
    eventual ``set_state`` (success -> readiness flip, failure -> "error")
    becomes visible.

    Tests monkeypatch this module-level symbol to ``await
    _run_wake_fn(fn, row)`` directly instead of backgrounding it, so the
    effect (``fake_runner.up_calls``, the row's new state, ...) is
    observable synchronously right after the request returns — see
    ``tests/test_data_apps_proxy.py``.
    """
    asyncio.create_task(_run_wake_fn(fn, row))


async def _trigger_wake(app_row: dict) -> None:
    """Wake a sleeping app — at most one in-flight attempt per app at a
    time, enforced by the ``dataapp:wake:{slug}`` lease. Callers that lose
    the race (another request/replica already holds the lease) just
    return — their caller renders the holding page regardless, and the
    already-in-progress wake will flip the state for everyone. The lease
    is deliberately never explicitly released on success/failure here —
    it's TTL-only, expiring naturally after ``_WAKE_LEASE_TTL_S`` seconds;
    an explicit release the instant this coroutine returns (well before
    the backgrounded redeploy actually finishes, for the recreate path)
    would let a second concurrent request re-acquire it and fire a
    duplicate wake while the first is still in flight.

    ``sleep_mode="pause"`` unpauses synchronously — cheap enough to await
    inline, so this coroutine sets ``running`` itself once it's done
    (rather than leaving that to the readiness-poll flip, which exists
    for the slower recreate path). ``sleep_mode="recreate"`` fires the
    full mint -> config -> ``runner.up`` pipeline
    (:func:`app.api.data_apps.redeploy_current`) via :func:`_spawn_wake` —
    NOT awaited to completion here, see that function's docstring.
    """
    slug = app_row["slug"]
    holder = default_holder_id()
    try:
        acquired = coordination().lease_acquire(f"dataapp:wake:{slug}", holder, ttl_s=_WAKE_LEASE_TTL_S)
    except CoordinationUnavailable:
        # Single-process dev fallback: no cross-process lock available —
        # proceed with the wake unlocked rather than leaving the app
        # stuck asleep forever because coordination happens to be down.
        acquired = True
    if not acquired:
        return  # another request/replica already owns this app's wake

    repo = data_apps_repo()
    if app_row.get("sleep_mode") == "pause":
        try:
            await run_in_threadpool(_runner().resume, slug)
        except (RunnerUnavailable, RunnerError) as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            repo.set_state(app_row["id"], "error", detail)
            return
        repo.set_state(app_row["id"], "running")
        return

    repo.set_state(app_row["id"], "deploying", "waking")
    await _spawn_wake(redeploy_current, app_row)


_STOPPED_HTML = """<!doctype html>
<title>App unavailable</title>
<style>body{{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0}}</style>
<div><h2>App is stopped</h2><p>This app ({state}) must be restarted by its owner or an
administrator before it can be reached — it does not wake on request.</p></div>
"""


def _not_running_response(slug: str, state: str, accepts_json: bool) -> Response:
    if accepts_json:
        return JSONResponse({"detail": "app_not_running", "state": state}, status_code=409)
    return Response(_STOPPED_HTML.format(state=state), media_type="text/html", status_code=409)


def _waking_response(request: Request, slug: str, accepts_json: bool) -> Response:
    if accepts_json:
        return JSONResponse({"status": "waking"}, status_code=503)
    from app.web.router import templates

    return templates.TemplateResponse(request, "data_app_waking.html", {"slug": slug}, status_code=503)


def _error_response(row: dict) -> Response:
    return JSONResponse(
        {"detail": "app_error", "state_detail": row.get("state_detail") or ""},
        status_code=409,
    )


async def _proxy(request: Request, slug: str, path: str) -> Response:
    """Stream-proxy one request to ``agnes-dataapp-<slug>``'s runtime
    container.

    Deliberately does NOT use ``async with _upstream_client() as client:
    ...; return StreamingResponse(...)`` (a shape that would close the
    client — and, dependent on the transport, its underlying connection —
    before the response body actually gets streamed by the ASGI server).
    Instead the client is closed from the same ``BackgroundTask`` that
    closes the upstream response, after the streamed body has been fully
    sent to the caller.
    """
    url = f"http://agnes-dataapp-{slug}:8888/{path}"
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in _CREDENTIAL_HEADERS
    }
    # A subdomain-origin request (rewritten by
    # app/data_apps_subdomain.py, which stamps this scope marker) serves
    # the app at its own root — there IS no prefix from its point of view,
    # unlike the `/apps/<slug>/...` path-prefix form of the same route.
    if not request.scope.get("agnes_data_app_subdomain"):
        headers["X-Forwarded-Prefix"] = f"/apps/{slug}"

    client = _upstream_client()
    try:
        upstream_request = client.build_request(
            request.method,
            url,
            headers=headers,
            params=request.query_params,
            content=request.stream(),
        )
        resp = await client.send(upstream_request, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # Connect-phase failures only — a mid-stream ReadTimeout on an
        # otherwise-reachable container is a different failure mode
        # (propagates as-is; the caller doesn't treat it as "container is
        # gone", just as a request that timed out).
        await client.aclose()
        raise
    except Exception:
        await client.aclose()
        raise

    async def _close() -> None:
        await resp.aclose()
        await client.aclose()

    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP},
        background=BackgroundTask(_close),
    )


@router.get("/apps/{slug}")
async def proxy_redirect_trailing_slash(slug: str, request: Request):
    qs = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/apps/{slug}/{qs}", status_code=307)


@router.api_route(
    "/apps/{slug}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy_app(slug: str, path: str, request: Request, user: dict = Depends(get_current_user)):
    """Excluded from OpenAPI (``include_in_schema=False``): a single FastAPI
    route registered against multiple HTTP methods shares ONE
    ``operation_id``/response-schema across all of them (FastAPI's
    ``generate_unique_id`` keys off ``list(route.methods)[0]``, not the
    method actually being documented) — every method would show identical,
    fictional response codes, and DELETE specifically can't honestly
    declare ``204`` (the status is whatever the proxied app's own DELETE
    handler returns, not something this route controls). This endpoint's
    shape is fundamentally undocumentable via a single static OpenAPI
    operation; behaviour is covered in ``tests/test_data_apps_proxy.py``
    instead of the docs/coverage ratchets that key off the schema.
    """
    _feature_gate()
    row = _get_row_or_404(slug)
    if not _can_view(user, row):
        raise HTTPException(status_code=403, detail="forbidden")

    _touch(row)

    state = row["state"]
    accepts_json = _wants_json(request)

    if state == "running":
        try:
            return await _proxy(request, slug, path)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            data_apps_repo().set_state(row["id"], "error", "container unreachable")
            raise HTTPException(status_code=502, detail="container_unreachable")

    if state == "sleeping":
        await _trigger_wake(row)
        return _waking_response(request, slug, accepts_json)

    if state == "deploying":
        return _waking_response(request, slug, accepts_json)

    if state in ("stopped", "created"):
        return _not_running_response(slug, state, accepts_json)

    if state == "error":
        return _error_response(row)

    # Defensive fallback for any future/unknown state value — fail closed
    # rather than silently proxying to a container the registry has no
    # confirmed-running belief about.
    return _not_running_response(slug, state, accepts_json)


def _ws_authenticate(websocket: WebSocket) -> Optional[dict]:
    """Resolve the caller for a WS handshake using the exact same
    session-cookie/PAT resolution as ``get_current_user`` — called
    directly (not via ``Depends``) because FastAPI's dependency solver
    only fills ``Request``-typed params from HTTP scopes; websocket routes
    in this codebase (``app/api/chat.py``, ``app/api/notifications_ws.py``)
    all authenticate by calling into the auth helper directly for the same
    reason. ``WebSocket`` duck-types every attribute ``get_current_user``
    actually touches (``.cookies``, ``.headers``, ``.state``), so passing
    it in place of a ``Request`` is safe.
    """
    from contextlib import contextmanager

    auth_header = websocket.headers.get("authorization")
    conn_cm = contextmanager(_get_db)
    try:
        with conn_cm() as conn:
            return get_current_user(request=websocket, authorization=auth_header, conn=conn)
    except HTTPException:
        return None


@router.websocket("/apps/{slug}/{path:path}")
async def proxy_ws(websocket: WebSocket, slug: str, path: str):
    from app.instance_config import get_data_apps_config

    if not get_data_apps_config().get("enabled"):
        await websocket.close(code=4404, reason="data_apps_disabled")
        return

    user = _ws_authenticate(websocket)
    if user is None:
        await websocket.close(code=4403, reason="forbidden")
        return

    row = data_apps_repo().get_by_slug(slug)
    if not row:
        await websocket.close(code=4404, reason="data_app_not_found")
        return

    if not _can_view(user, row):
        await websocket.close(code=4403, reason="forbidden")
        return

    if row["state"] != "running":
        await websocket.close(code=4404, reason="app_not_running")
        return

    _touch(row)

    await websocket.accept()

    query = f"?{websocket.url.query}" if websocket.url.query else ""
    upstream_url = f"ws://agnes-dataapp-{slug}:8888/{path}{query}"

    import websockets

    # No caller headers (incl. `Authorization`/`Cookie`) are forwarded to the
    # upstream handshake at all — same credential-hygiene guarantee as the
    # HTTP proxy's `_CREDENTIAL_HEADERS` strip, just trivially satisfied here
    # since this bridge never builds a header dict from `websocket.headers`
    # in the first place.
    try:
        async with websockets.connect(upstream_url) as upstream:

            async def client_to_upstream() -> None:
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        text = message.get("text")
                        data = message.get("bytes")
                        if text is not None:
                            await upstream.send(text)
                        elif data is not None:
                            await upstream.send(data)
                finally:
                    await upstream.close()

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                finally:
                    try:
                        await websocket.close()
                    except Exception:
                        pass

            await asyncio.gather(client_to_upstream(), upstream_to_client(), return_exceptions=True)
    except Exception:
        # Broad on purpose: covers plain connect failures (OSError — refused/
        # unreachable/DNS) as well as the `websockets` library's own
        # handshake-rejection exceptions (e.g. `InvalidStatus`/
        # `InvalidHandshake` when the container answers but not with a
        # valid WS upgrade). Any of these means the bridge never got a
        # usable upstream connection — close gracefully rather than let an
        # unhandled exception surface as a raw 500-equivalent.
        logger.warning("WS bridge to data app %s failed", slug, exc_info=True)
        try:
            await websocket.close(code=1011, reason="upstream_unreachable")
        except Exception:
            pass
