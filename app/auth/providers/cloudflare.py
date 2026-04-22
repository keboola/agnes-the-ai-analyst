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
from typing import Optional

import jwt as pyjwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

_JWKS_CLIENT: Optional[PyJWKClient] = None
_JWKS_TEAM: Optional[str] = None  # team string the cached client was built for


def _team() -> str:
    return os.environ.get("CF_ACCESS_TEAM", "")


def _aud() -> str:
    return os.environ.get("CF_ACCESS_AUD", "")


def is_available() -> bool:
    """Provider is active only when BOTH team and aud are configured."""
    return bool(_team() and _aud())


def _jwks_url() -> str:
    return f"https://{_team()}.cloudflareaccess.com/cdn-cgi/access/certs"


def _issuer() -> str:
    return f"https://{_team()}.cloudflareaccess.com"


def _get_jwks_client() -> PyJWKClient:
    """Lazy-init JWKS client. PyJWKClient caches keys with 5-min TTL by default.

    If `CF_ACCESS_TEAM` changes (e.g. between tests), rebuild the client.
    """
    global _JWKS_CLIENT, _JWKS_TEAM
    current_team = _team()
    if _JWKS_CLIENT is None or _JWKS_TEAM != current_team:
        _JWKS_CLIENT = PyJWKClient(_jwks_url(), cache_jwk_set=True, lifespan=300)
        _JWKS_TEAM = current_team
    return _JWKS_CLIENT


def verify_cf_jwt(token: str) -> Optional[dict]:
    """Verify a Cloudflare Access JWT. Returns claims dict on success, None on any failure.

    Never raises — all exceptions are logged at debug and mapped to None so the
    middleware can treat them as "pass through to normal auth."
    """
    if not is_available():
        return None
    if not token:
        return None
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=_aud(),
            issuer=_issuer(),
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
        return claims
    except pyjwt.InvalidTokenError as e:
        logger.debug("CF Access JWT invalid: %s", e)
        return None
    except Exception as e:
        # JWKS fetch failure, network error, etc. — never propagate
        logger.warning("CF Access JWT verification error: %s", e)
        return None
