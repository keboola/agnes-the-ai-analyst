"""Trusted client-IP derivation for auth throttling + audit (security audit F9).

The naive "leftmost X-Forwarded-For hop" is fully attacker-controlled: a client
can send ``X-Forwarded-For: <anything>`` and, when a reverse proxy *appends*
(rather than resets) the header, the spoofed value ends up on the LEFT. Keying
the per-IP rate-limit bucket on that value lets an attacker land in a fresh
bucket every request and defeat the auth throttles entirely (unbounded
password brute-force, email-bomb / SendGrid-quota burn).

Correct model: behind ``AGNES_TRUSTED_PROXY_HOPS`` trusted reverse proxies
(default 1 — the shipped Caddy front), the real client IP is the hop that the
*outermost trusted proxy* observed, i.e. the ``hops``-th entry counting from the
RIGHT of the chain. Everything to the left of it is client-supplied and
untrusted. Caddy appends the immediate peer to XFF, so with one trusted proxy
the rightmost hop is the genuine client and any spoofed prefix is ignored.

Operators running N chained trusted proxies set ``AGNES_TRUSTED_PROXY_HOPS=N``.
When the app is exposed directly (no proxy) there is no XFF and we fall back to
the connection peer, which is authentic.
"""

from __future__ import annotations

import os
from typing import Optional

from starlette.requests import Request


def _trusted_hops() -> int:
    """Number of trusted reverse-proxy hops in front of the app (>= 1)."""
    raw = os.environ.get("AGNES_TRUSTED_PROXY_HOPS", "1")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def trusted_client_ip(request: Optional[Request]) -> Optional[str]:
    """Return the request's client IP, trusting only ``AGNES_TRUSTED_PROXY_HOPS``
    rightmost X-Forwarded-For hops.

    Value is used for auth rate-limiting keys and for audit/diagnostics
    (``personal_access_tokens.last_used_ip``, ``audit_log``) — never for
    authorization decisions.
    """
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            hops = _trusted_hops()
            # The client the outermost trusted proxy saw is `hops` from the end.
            idx = len(parts) - hops
            if idx < 0:
                idx = 0
            return parts[idx] or None
    client = getattr(request, "client", None)
    return getattr(client, "host", None) if client else None
