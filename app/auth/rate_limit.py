"""Per-IP rate limiting for auth endpoints (#45).

Why: every auth endpoint was unthrottled before this module — `grep -r
"slowapi\\|limiter\\|throttle"` returned zero hits in app/. That left
``/auth/password/login`` and ``/auth/token`` open to password brute-force
and ``/auth/email/send-link`` open to SMTP/SendGrid email-bombing
(attacker loops with random recipients and burns through quota).

How: slowapi installs a starlette middleware that rejects with 429 when
the per-route ``@limiter.limit("N/period")`` decorator is exceeded. The
key is the client IP, taken from the leftmost X-Forwarded-For hop (Caddy
in front of the app strips client-supplied XFF and sets its own — same
trust model as ``app.auth.dependencies._client_ip``).

Operator override: set ``AGNES_AUTH_RATELIMIT_ENABLED=0`` and restart
the process (no image rebuild needed — flip the env in the compose
``.env`` / systemd unit and bounce the container). The value is read at
process start because the slowapi ``Limiter`` constructor freezes
``enabled`` at import; that limitation is fine in practice because
Agnes's other env knobs already require a process restart to take
effect (see ``.env_overlay`` loader in ``app/main.py`` for the same
shape — file-based overlay merged at startup, no live reload).

The test suite flips ``limiter.enabled`` directly via an autouse
conftest fixture (no restart required because tests share a process)
and re-enables only inside the dedicated rate-limit test, so
generous-but-finite limits don't bleed into other test files that
hammer auth endpoints in tight loops.
"""

from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse


def _client_ip_key(request: Request) -> str:
    """IP key, preferring leftmost X-Forwarded-For hop.

    Mirrors ``app.auth.dependencies._client_ip`` — same Caddy-in-front
    trust model. If the app is ever exposed directly to the internet
    without a proxy, the XFF header becomes client-settable and an
    attacker can rotate the per-IP bucket trivially. Document that
    deployment shape in the runbook before flipping it on.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        ip = xff.split(",", 1)[0].strip()
        if ip:
            return ip
    return get_remote_address(request)


def _enabled_default() -> bool:
    return os.environ.get("AGNES_AUTH_RATELIMIT_ENABLED", "1").lower() not in (
        "0", "false", "no", "off",
    )


# Module-level singleton — slowapi binds storage at construction and the
# decorators capture this exact instance at import time. Tests toggle
# ``limiter.enabled`` and call ``limiter.reset()`` between cases.
#
# headers_enabled is intentionally OFF: when on, slowapi injects
# X-RateLimit-* headers via a per-handler response parameter, which forces
# every decorated endpoint to add ``response: Response`` even on the happy
# path. The protection here is the 429 with Retry-After (still emitted by
# the exception handler below) — the diagnostic headers on success
# responses are not worth the API-shape churn across 5 endpoints.
limiter = Limiter(
    key_func=_client_ip_key,
    enabled=_enabled_default(),
    headers_enabled=False,
    default_limits=[],
)


async def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Match Agnes's existing JSON error shape (``{"detail": "..."}``)
    instead of slowapi's text/plain default — keeps the CLI / web error
    parser uniform across all 4xx responses.
    """
    return JSONResponse(
        {"detail": f"Too many requests — {exc.detail}"},
        status_code=429,
        headers={"Retry-After": "60"},
    )


__all__ = [
    "limiter",
    "RateLimitExceeded",
    "SlowAPIMiddleware",
    "_rate_limit_exceeded_handler",
]
