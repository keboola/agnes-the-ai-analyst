"""Public Agnes origin for OAuth discovery, redirects, and connector metadata."""

from __future__ import annotations

import os

from starlette.requests import Request


def pinned_public_base_url() -> str | None:
    """Return an operator-pinned public origin, if configured."""
    for key in ("AGNES_BASE_URL", "SERVER_URL"):
        val = os.environ.get(key)
        if val:
            return val.rstrip("/")
    return None


def public_base_url(*, request: Request | None = None) -> str:
    """Public HTTPS/HTTP origin for this Agnes instance (no trailing slash).

    Resolution order:
    1. ``AGNES_BASE_URL`` (connector / Cowork bundles)
    2. ``SERVER_URL`` (general external links)
    3. Incoming request ``base_url`` (proxy-aware when env is unset)
    4. ``http://localhost:8000`` (local dev fallback)
    """
    pinned = pinned_public_base_url()
    if pinned:
        return pinned
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://localhost:8000"


def mcp_issuer_url(*, request: Request | None = None) -> str:
    """OAuth issuer / MCP resource URL for the streamable connector."""
    return f"{public_base_url(request=request)}/api/mcp/http"
