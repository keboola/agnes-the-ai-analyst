"""Bot Framework Connector Service JWT verification.

Unlike Slack (symmetric HMAC over the raw body with a shared signing
secret — see ``services/slack_bot/sigverify.py``), every inbound Bot
Framework Activity POST carries ``Authorization: Bearer <JWT>`` signed
with Microsoft's private key (RS256). Verifying it means:

1. Read the token's ``kid`` header (no signature check yet).
2. Resolve Bot Framework's JWKS via its OpenID Connect metadata document
   (``jwks_uri``) — fetched rather than hardcoded so a Microsoft-side key
   rotation or endpoint change never requires a code change, at the cost
   of one extra request on a JWKS cache miss. A fallback default is kept
   for the (documented, stable) case where that fetch itself fails.
3. Find the JWK matching ``kid``, convert it to a key PyJWT can verify
   with, and validate signature + ``aud``/``iss``/``exp``/``nbf``.

Spec: https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

logger = logging.getLogger(__name__)

BOTFRAMEWORK_OPENID_CONFIG_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
# Fallback used only if the openid-configuration fetch above fails — this is
# the stable, documented JWKS endpoint Bot Framework's metadata resolves to.
BOTFRAMEWORK_JWKS_URI_DEFAULT = "https://login.botframework.com/v1/.well-known/keys"
BOTFRAMEWORK_ISSUER = "https://api.botframework.com"

# Module-level JWKS cache, keyed by `kid`. Populated on first use and
# refreshed on a `kid` cache-miss so Microsoft's periodic key rotation is
# handled without a restart.
_JWKS_CACHE: dict[str, dict[str, Any]] = {}
# Throttle guard: minimum time between refetches so an attacker sending a
# stream of requests with bogus `kid` values can't force a network round
# trip on every single one. `0.0` (never fetched yet) always refetches.
_LAST_REFETCH_AT: float = 0.0
_MIN_REFETCH_INTERVAL_SECONDS = 60.0


def _http_client() -> httpx.AsyncClient:
    """Seam for tests: substitute an ``httpx.MockTransport``-backed client."""
    return httpx.AsyncClient(timeout=10)


async def _resolve_jwks_uri(client: httpx.AsyncClient) -> str:
    try:
        resp = await client.get(BOTFRAMEWORK_OPENID_CONFIG_URL)
        resp.raise_for_status()
        jwks_uri = resp.json().get("jwks_uri")
        if jwks_uri:
            return jwks_uri
    except Exception:
        logger.warning(
            "failed to fetch Bot Framework openid-configuration; falling back to default jwks_uri",
            exc_info=True,
        )
    return BOTFRAMEWORK_JWKS_URI_DEFAULT


async def _refresh_jwks_cache() -> None:
    """Refetch the JWKS document and replace the module-level `kid` cache.

    Never raises — a failure just leaves the existing cache in place (an
    unknown `kid` then fails verification, which is the fail-closed
    behavior we want).
    """
    global _LAST_REFETCH_AT
    _LAST_REFETCH_AT = time.monotonic()
    try:
        async with _http_client() as client:
            jwks_uri = await _resolve_jwks_uri(client)
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            keys = resp.json().get("keys", [])
    except Exception:
        logger.warning("failed to fetch Bot Framework JWKS", exc_info=True)
        return
    _JWKS_CACHE.clear()
    for jwk in keys:
        kid = jwk.get("kid")
        if kid:
            _JWKS_CACHE[kid] = jwk


async def verify_bot_framework_token(authorization_header: str | None, app_id: str) -> bool:
    """Verify a Bot Framework Connector ``Authorization`` header.

    Fails closed (returns ``False``, never raises) on a missing/malformed
    header, unknown key id, expired token, wrong audience, wrong issuer, or
    bad signature.
    """
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return False
    token = authorization_header[len("Bearer ") :].strip()
    if not token:
        return False

    try:
        unverified_header = jwt.get_unverified_header(token)
    except Exception:
        return False
    kid = unverified_header.get("kid")
    if not kid:
        return False

    if kid not in _JWKS_CACHE:
        never_fetched = not _JWKS_CACHE and _LAST_REFETCH_AT == 0.0
        stale_enough = (time.monotonic() - _LAST_REFETCH_AT) >= _MIN_REFETCH_INTERVAL_SECONDS
        if never_fetched or stale_enough:
            await _refresh_jwks_cache()
        if kid not in _JWKS_CACHE:
            return False

    try:
        public_key = RSAAlgorithm.from_jwk(json.dumps(_JWKS_CACHE[kid]))
        jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],  # never derive from the token's own `alg` header
            audience=app_id,
            issuer=BOTFRAMEWORK_ISSUER,
        )
    except Exception:
        return False
    return True
