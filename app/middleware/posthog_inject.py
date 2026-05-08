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
    * Streaming responses (we'd have to materialise them — not worth it
      for big downloads).
    * Responses that already contain ``posthog.init`` (defensive — keeps
      base-extending templates from getting a double-injection if a
      future change re-includes the partial there).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


_HEAD_CLOSE = b"</head>"


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

        # BaseHTTPMiddleware materialises the body for us via response.body_iterator.
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            chunks.append(chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode("utf-8"))
        body = b"".join(chunks)

        if _HEAD_CLOSE not in body or b"posthog.init" in body:
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        try:
            snippet = _render_snippet(request)
        except Exception:
            logger.exception("PostHog snippet render failed; serving response unmodified")
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        body = body.replace(_HEAD_CLOSE, snippet.encode("utf-8") + _HEAD_CLOSE, 1)
        # content-length must reflect the rewritten body — Starlette's
        # ``Response`` sets it for us when we drop the prior header.
        new_headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(
            content=body,
            status_code=response.status_code,
            headers=new_headers,
            media_type=response.media_type,
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
