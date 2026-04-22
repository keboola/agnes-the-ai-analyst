"""Cloudflare Access auth provider — verifies edge JWT from Cloudflare Zero Trust.

Unlike password/google/email providers, Cloudflare Access is NOT a clickable
login button. Cloudflare's edge gate injects a signed JWT in the
`Cf-Access-Jwt-Assertion` header on every request. The app trusts that JWT
(after verifying signature + audience) and auto-provisions the user, issuing
our standard `access_token` cookie so downstream route handlers work unchanged.

This module exposes pure functions; the request-interception logic lives in
`app/auth/middleware.py`.
"""

import logging
import os

logger = logging.getLogger(__name__)


def _team() -> str:
    return os.environ.get("CF_ACCESS_TEAM", "")


def _aud() -> str:
    return os.environ.get("CF_ACCESS_AUD", "")


def is_available() -> bool:
    """Provider is active only when BOTH team and aud are configured.

    The two-env-var gate prevents header spoofing on deployments that don't
    sit behind Cloudflare — an attacker could otherwise forge
    `Cf-Access-Jwt-Assertion` and bypass auth.

    Env vars are read at call time (not cached at import) so tests and
    runtime env changes behave predictably.
    """
    return bool(_team() and _aud())
