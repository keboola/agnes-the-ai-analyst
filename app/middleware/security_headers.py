"""App-level security-response-headers middleware.

Sets baseline security headers on every response so protection does not depend
on the bundled Caddy reverse proxy: a deployment behind a different TLS
terminator (a supported topology) would otherwise serve the entire
authenticated UI with no clickjacking / MIME-sniffing / transport-downgrade
defenses and no CSP. Behind Caddy these are simply re-affirmed — Caddy sets the
same values at the edge, so there is no conflict.

Implemented as a PURE ASGI middleware (not ``BaseHTTPMiddleware``) so it does
not buffer response bodies — the app serves SSE/streamable-MCP responses, which
``BaseHTTPMiddleware`` would break. Headers are added on ``http.response.start``
with ``setdefault`` so a route that sets its own value (e.g. the hardened
marketplace endpoint's stricter CSP) is never overridden.

The Content-Security-Policy here is deliberately a NON-breaking subset
(``frame-ancestors`` / ``object-src`` / ``base-uri``): it blocks framing,
plugin/object embeds and ``<base>`` hijacking without constraining script /
style / img / connect sources, so it cannot break the existing inline-styled
dashboard pages. A full nonce-based ``script-src`` CSP is tracked as a follow-up.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders

# Non-breaking CSP subset — see module docstring.
_CSP = "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"

_STATIC_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "content-security-policy": _CSP,
}

_HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    """Inject baseline security headers on every HTTP response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # scope["scheme"] reflects X-Forwarded-Proto when uvicorn runs with
        # --proxy-headers, so HSTS is emitted whenever the edge served HTTPS.
        is_https = scope.get("scheme") == "https"

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in _STATIC_HEADERS.items():
                    headers.setdefault(key, value)
                if is_https:
                    headers.setdefault("strict-transport-security", _HSTS)
            await send(message)

        await self.app(scope, receive, send_wrapper)
