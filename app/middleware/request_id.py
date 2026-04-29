"""Request-ID middleware. Assigns or propagates X-Request-ID per request.

Pure ASGI middleware (not BaseHTTPMiddleware) so the request_id ContextVar
propagates into route handlers and BackgroundTasks without being clobbered
by an early `finally`-block reset. Each request runs in its own asyncio
task with an isolated context copy, so no manual reset is needed.
"""

from __future__ import annotations

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_config import request_id_var

_MAX_RID_LEN = 64
_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def _sanitize(rid: str) -> str:
    cleaned = "".join(c for c in rid if c in _ALLOWED)[:_MAX_RID_LEN]
    return cleaned or uuid.uuid4().hex[:12]


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid_header: str | None = None
        for k, v in scope.get("headers", []):
            if k == b"x-request-id":
                rid_header = v.decode("latin-1", errors="replace")
                break
        rid = _sanitize(rid_header) if rid_header else uuid.uuid4().hex[:12]
        request_id_var.set(rid)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
