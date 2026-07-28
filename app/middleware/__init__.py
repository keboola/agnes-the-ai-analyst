"""Shared middleware constants.

SSE_BYPASS_PREFIXES: response paths that stream ``text/event-stream`` and
must skip every ``BaseHTTPMiddleware`` layer — that base class buffers the
full response body, which re-collapses an SSE stream into one end-of-turn
burst (and on Python 3.13 raises AssertionError on the second
``http.response.start`` message). Any middleware subclassing
``BaseHTTPMiddleware`` must consult this tuple in ``__call__`` and fall
through to the bare ASGI app for matching paths; the gzip skip-list in
``app/main.py`` mirrors the same paths for the same reason.
"""

SSE_BYPASS_PREFIXES: tuple[str, ...] = (
    "/api/mcp",
    "/api/broker/anthropic",
)
