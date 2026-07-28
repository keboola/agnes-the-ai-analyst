"""Pure-ASGI middleware stamping the audit-timing contextvar.

Deliberately NOT a ``BaseHTTPMiddleware`` — this app streams SSE through
its middleware stack (see the GZip/broker-SSE incidents) and the pure ASGI
form adds zero buffering or task-group overhead to the hot path.
"""

from __future__ import annotations

from src.audit_context import mark_request_start


class AuditTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            mark_request_start()
        await self.app(scope, receive, send)
