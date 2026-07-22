"""Pure-ASGI middleware: rewrite ``<slug>.<subdomain_base>`` host requests
to ``/apps/<slug>/...`` paths (Task 8 — ingress proxy + wake-on-request).

Only active when ``data_apps.subdomain_base`` is configured in
``instance.yaml`` (e.g. ``apps.example.com``). A request whose ``Host``
header is ``s.apps.example.com`` gets its ``scope["path"]`` rewritten to
``/apps/s`` + the original path, then falls through to the normal
routing table — landing on ``app.api.data_apps_proxy``'s catch-all route
exactly as if the caller had hit ``https://<main-host>/apps/s/...``
directly. When ``subdomain_base`` is unset (the default), this middleware
is a no-op passthrough.

Deliberately a plain callable class (not ``BaseHTTPMiddleware``) so it
can inspect/rewrite ``scope`` before ASGI routing without buffering the
request/response bodies — a data-app's WebSocket traffic and the proxy's
streamed HTTP responses (``app/api/data_apps_proxy.py``) must never be
fully buffered in memory.
"""

from __future__ import annotations


class DataAppSubdomainMiddleware:
    """Rewrite ``<slug>.<base>`` host requests to ``/apps/<slug>/...`` paths."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            from app.instance_config import get_data_apps_config

            # `get_data_apps_config()` returns the `data_apps:` instance.yaml
            # block, defaulting to `{}` when the key is absent — but an
            # instance.yaml that explicitly sets `data_apps:` to a null/empty
            # value (or a config-not-loaded-yet state some test/bootstrap
            # paths hit) can still surface `None` here. This middleware runs
            # on EVERY request (including `/metrics`, `/healthz`, etc.), so a
            # bad config must degrade to "middleware is a no-op", never crash
            # the whole app.
            base = ((get_data_apps_config() or {}).get("subdomain_base") or "").strip(".")
            if base:
                host = dict(scope.get("headers") or {}).get(b"host", b"").decode().split(":")[0]
                if host.endswith("." + base):
                    slug = host[: -(len(base) + 1)]
                    if "." not in slug:
                        scope = dict(scope)
                        scope["path"] = f"/apps/{slug}" + scope["path"]
        await self.app(scope, receive, send)
