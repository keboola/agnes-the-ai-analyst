"""BigQuery auth helper — fetch ephemeral access token from GCE metadata server.

Used by the BQ extractor and orchestrator when running on GCE with a service
account attached to the VM. No key file required.

Tokens are cached in process memory until they expire (with a 60s safety
buffer), so consumers like ``src/db.py::_reattach_remote_extensions`` —
which runs on every readonly conn open — don't pay the metadata-server
round-trip cost on every request.
"""

import json
import logging
import threading
import time
import urllib.request
import urllib.error
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)
_METADATA_TIMEOUT_S = 5
# Refresh the cached token this many seconds before its declared expiry.
# 60 s is enough to cover slow callers without making the cache too noisy.
_CACHE_SAFETY_BUFFER_S = 60

# Module-level cache: (token, expiry_monotonic) or None.
_token_cache: Optional[Tuple[str, float]] = None
_cache_lock = threading.Lock()


class BQMetadataAuthError(RuntimeError):
    """Raised when GCE metadata token cannot be obtained."""


def clear_token_cache() -> None:
    """Drop the cached token so the next get_metadata_token() forces a refetch.

    Useful in tests and after authoritative auth failures (e.g. 401 from BQ).
    """
    global _token_cache
    with _cache_lock:
        _token_cache = None


def _fetch_metadata_token() -> Tuple[str, int]:
    """Make the actual HTTP call. Returns ``(access_token, expires_in_seconds)``.

    Raises ``BQMetadataAuthError`` on any failure.
    """
    req = urllib.request.Request(
        _METADATA_TOKEN_URL,
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_METADATA_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise BQMetadataAuthError(f"metadata server unreachable: {e}") from e
    except json.JSONDecodeError as e:
        raise BQMetadataAuthError(f"metadata response not JSON: {e}") from e

    token = payload.get("access_token")
    if not token:
        raise BQMetadataAuthError("no access_token in response")
    # GCE metadata server always returns expires_in (seconds). Default 0 if
    # missing so a malformed response doesn't poison the cache for an hour.
    try:
        expires_in = int(payload.get("expires_in", 0) or 0)
    except (TypeError, ValueError):
        expires_in = 0
    return token, expires_in


def get_metadata_token() -> str:
    """Return an access token from the GCE metadata server, cached in process.

    Returns the cached token when ``time.monotonic() < cached_expiry`` (with a
    60-second safety buffer applied at fetch time). Refreshes otherwise.

    Raises:
        BQMetadataAuthError: if the metadata server is unreachable or the
            response is malformed.
    """
    global _token_cache
    now = time.monotonic()

    with _cache_lock:
        cached = _token_cache
    if cached is not None and now < cached[1]:
        return cached[0]

    token, expires_in = _fetch_metadata_token()
    # Only cache if expires_in is meaningfully large; if the response lacks the
    # field or returns 0/negative, skip caching so the next call retries.
    if expires_in > _CACHE_SAFETY_BUFFER_S:
        new_expiry = time.monotonic() + (expires_in - _CACHE_SAFETY_BUFFER_S)
        with _cache_lock:
            _token_cache = (token, new_expiry)
    return token
