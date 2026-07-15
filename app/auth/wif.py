"""Workload Identity Federation (WIF) token exchange — keyless Anthropic auth.

Opt-in alternative to a static ``ANTHROPIC_API_KEY``: mint a short-lived
Anthropic access token from the workload's own OIDC identity via Anthropic's
native WIF (``POST /v1/oauth/token``, the RFC 7523 ``jwt-bearer`` grant). This is
**server-side only** — used by the chat broker's Anthropic proxy
(``app/api/broker.py``); the minted token never enters the sandbox, so the chat
sandbox secret-broker isolation (INC-01572) is preserved and strengthened (there
is no long-lived key anywhere).

Reads the same environment contract the official Anthropic SDK uses, so a
deployment configured for the SDK's WIF path works here unchanged:

- ``ANTHROPIC_FEDERATION_RULE_ID`` (``fdrl_...``)
- ``ANTHROPIC_ORGANIZATION_ID`` (org UUID)
- ``ANTHROPIC_SERVICE_ACCOUNT_ID`` (``svac_...``)
- ``ANTHROPIC_WORKSPACE_ID`` (``wrkspc_...`` or ``default``; optional — required
  only when the federation rule spans multiple workspaces)
- ``ANTHROPIC_IDENTITY_TOKEN`` **or** ``ANTHROPIC_IDENTITY_TOKEN_FILE`` — the
  OIDC JWT the operator's identity source provides (a projected SA-token file, a
  cloud metadata identity token written to a file, etc.). The file is re-read on
  every exchange so rotating projected tokens stay current.

The access token is cached with its expiry (module-level, lock-guarded) and
refreshed shortly before it expires — mirroring ``connectors/bigquery/auth.py``.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional, Tuple

import httpx

_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_EXCHANGE_TIMEOUT_S = 10.0
# Refresh this many seconds before the declared expiry so an in-flight request
# never races the boundary.
_CACHE_SAFETY_BUFFER_S = 60
# When the exchange response omits ``expires_in`` (or it's zero), cache this
# briefly so a malformed response can't pin a stale token for an hour or spin a
# hot re-exchange loop.
_FALLBACK_TTL_S = 30

# (access_token, expiry_monotonic) or None.
_cache: Optional[Tuple[str, float]] = None
_lock = threading.Lock()


class WIFAuthError(RuntimeError):
    """Raised when a federated Anthropic access token cannot be obtained."""


def clear_token_cache() -> None:
    """Drop the cached token so the next call forces a fresh exchange.

    Call after an authoritative 401 from Anthropic — the cached token may have
    been revoked before its declared expiry.
    """
    global _cache
    with _lock:
        _cache = None


def _read_identity_token() -> str:
    tok = os.environ.get("ANTHROPIC_IDENTITY_TOKEN", "").strip()
    if tok:
        return tok
    path = os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE", "").strip()
    if path:
        try:
            # Re-read every exchange: projected tokens rotate on disk.
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as e:
            raise WIFAuthError(f"identity token file unreadable ({path}): {e}") from e
    raise WIFAuthError("no ANTHROPIC_IDENTITY_TOKEN or ANTHROPIC_IDENTITY_TOKEN_FILE set")


def _exchange() -> Tuple[str, int]:
    """Perform the token exchange. Returns ``(access_token, expires_in_seconds)``."""
    rule = os.environ.get("ANTHROPIC_FEDERATION_RULE_ID", "").strip()
    org = os.environ.get("ANTHROPIC_ORGANIZATION_ID", "").strip()
    svc = os.environ.get("ANTHROPIC_SERVICE_ACCOUNT_ID", "").strip()
    if not (rule and org and svc):
        raise WIFAuthError(
            "workload_identity auth requires ANTHROPIC_FEDERATION_RULE_ID, "
            "ANTHROPIC_ORGANIZATION_ID, and ANTHROPIC_SERVICE_ACCOUNT_ID to be set"
        )
    body = {
        "grant_type": _JWT_BEARER_GRANT,
        "assertion": _read_identity_token(),
        "federation_rule_id": rule,
        "organization_id": org,
        "service_account_id": svc,
    }
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if workspace:
        body["workspace_id"] = workspace
    try:
        resp = httpx.post(_TOKEN_URL, json=body, timeout=_EXCHANGE_TIMEOUT_S)
    except httpx.HTTPError as e:
        raise WIFAuthError(f"token exchange request failed: {e}") from e
    if resp.status_code != 200:
        # invalid_grant causes are logged server-side only, so echo the body head
        # (never the identity token — it's request-side, not in the response).
        raise WIFAuthError(f"token exchange failed: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise WIFAuthError(f"token exchange response not JSON: {e}") from e
    token = data.get("access_token")
    if not token:
        raise WIFAuthError("token exchange response missing access_token")
    try:
        expires_in = int(data.get("expires_in", 0) or 0)
    except (TypeError, ValueError):
        expires_in = 0
    return token, expires_in


def get_federated_access_token() -> str:
    """Return a cached federated Anthropic access token, minting/refreshing as needed.

    Raises ``WIFAuthError`` if the token cannot be obtained.
    """
    global _cache
    with _lock:
        if _cache is not None:
            token, expiry = _cache
            if time.monotonic() < expiry:
                return token
        token, expires_in = _exchange()
        ttl = max(expires_in - _CACHE_SAFETY_BUFFER_S, _FALLBACK_TTL_S)
        _cache = (token, time.monotonic() + ttl)
        return token
