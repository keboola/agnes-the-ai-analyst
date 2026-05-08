"""HTML-injection middleware that places the PostHog snippet in every page.

Many of this app's Jinja templates are standalone (their own ``<!DOCTYPE
html>``) and do not extend ``base.html`` / ``base_login.html`` — including
the dashboard, catalog, admin pages, and activity center. Adding
``{% include '_posthog.html' %}`` to each one is fragile and easy to miss.

Instead, this middleware rewrites every HTML response to inject the
rendered snippet immediately before ``</head>``. When PostHog is disabled
(no ``POSTHOG_API_KEY``) the middleware is a no-op.

Skips:
    * Non-HTML responses (everything API, JSON, parquet, CSV).
    * Responses larger than ``_MAX_BUFFER_BYTES`` — defends against
      genuine HTML streams (rare but legal: large dashboards rendered
      as chunked transfer) where buffering the entire body would balloon
      memory. Snippet injection is best-effort.
    * Responses that already contain ``posthog.init`` (defensive — keeps
      base-extending templates from getting a double-injection if a
      future change re-includes the partial there).

Background tasks attached to a route via ``Response.background`` are
preserved on every return path. ``BaseHTTPMiddleware`` materialises the
body and asks subclasses to return a fresh ``Response``; forgetting to
forward ``background`` would silently cancel any deferred work the
handler scheduled (audit logging, async webhooks, deferred email sends),
with no log line. Caught in PR #231 review (minasarustamyan).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


_HEAD_CLOSE = b"</head>"

# Hard ceiling on how much body we're willing to buffer in memory just to
# inject ~3 KB of snippet. 4 MB covers every HTML page this app currently
# emits with ample headroom while preventing a pathological streamed-HTML
# response from ballooning RSS. Adjust if a legitimate page exceeds it.
_MAX_BUFFER_BYTES = 4 * 1024 * 1024


def _passthrough(body: bytes, response: Response) -> Response:
    """Return a fresh ``Response`` carrying ``body`` plus every attribute of
    ``response`` that ``BaseHTTPMiddleware`` would otherwise drop —
    importantly ``background`` so any ``BackgroundTask`` /
    ``BackgroundTasks`` the handler attached still fires.
    """
    return Response(
        content=body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
        background=response.background,
    )


class PosthogInjectionMiddleware(BaseHTTPMiddleware):
    """Inject the PostHog snippet into every HTML response."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        from src.observability import get_posthog
        if not get_posthog().enabled:
            return await call_next(request)

        response = await call_next(request)

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type.lower():
            return response

        # Buffer the body. ``BaseHTTPMiddleware`` consumes
        # ``response.body_iterator`` here — once we iterate it, the only
        # way to forward the response is to return a new one. Bail out
        # past ``_MAX_BUFFER_BYTES`` so a streamed HTML response (rare but
        # legal) doesn't balloon memory.
        chunks: list[bytes] = []
        total = 0
        too_big = False
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            buf = chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode("utf-8")
            total += len(buf)
            if total > _MAX_BUFFER_BYTES:
                too_big = True
                # Still need to drain the iterator to avoid breaking the
                # ASGI stream contract; but stop appending so we don't
                # hold every chunk.
                continue
            chunks.append(buf)
        if too_big:
            logger.warning(
                "PostHog snippet injection skipped: HTML response > %d bytes (path=%s)",
                _MAX_BUFFER_BYTES, request.url.path,
            )
            # We've consumed the iterator; rebuild from the chunks we
            # captured before the cap. Better to serve a truncated body
            # than to crash, but in practice the cap is set so this
            # branch shouldn't fire for legitimate pages.
            return _passthrough(b"".join(chunks), response)

        body = b"".join(chunks)

        if _HEAD_CLOSE not in body or b"posthog.init" in body:
            return _passthrough(body, response)

        try:
            snippet = _render_snippet(request)
        except Exception:
            logger.exception("PostHog snippet render failed; serving response unmodified")
            return _passthrough(body, response)

        body = body.replace(_HEAD_CLOSE, snippet.encode("utf-8") + _HEAD_CLOSE, 1)
        # content-length must reflect the rewritten body — Starlette's
        # ``Response`` sets it for us when we drop the prior header.
        new_headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(
            content=body,
            status_code=response.status_code,
            headers=new_headers,
            media_type=response.media_type,
            background=response.background,
        )


def _render_snippet(request: Request) -> str:
    """Render ``_posthog.html`` with the current request's identify state."""
    from app.web.router import templates, _posthog_user_block, _posthog_config_global

    cfg = _posthog_config_global()
    user_block = _posthog_user_block(request)

    template = templates.get_template("_posthog.html")
    return template.render(
        request=request,
        posthog_config=cfg,
        # ``_posthog.html`` calls ``posthog_user_block(request)`` itself —
        # provide the same callable so the template renders identically
        # to the inline-include path.
        posthog_user_block=lambda _r: user_block,
    )
