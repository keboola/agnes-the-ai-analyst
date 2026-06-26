"""BigQuery auth helper — fetch ephemeral access token.

Resolution order (first one that returns a token wins):

1. **GOOGLE_APPLICATION_CREDENTIALS / gcloud ADC** — env-pinned SA key file
   or ``gcloud auth application-default login`` credentials. Covered by the
   existing ``_fetch_adc_token`` path which honours
   ``GOOGLE_APPLICATION_CREDENTIALS`` before falling back to gcloud ADC.
2. **Vault ``BIGQUERY_SERVICE_ACCOUNT_JSON``** — admin-pasted SA key JSON,
   stored encrypted in ``system_secrets``. In-memory only — the key is never
   written to disk. Wins over the VM's ambient identity deliberately: an admin
   who pastes a key via the UI expects it to take effect on non-GCP hosts.
3. **GCE metadata server** — production path on a GCP VM with an attached
   service account. Fast (no library overhead) and what most GCE deployments
   use when no explicit key is provided.
4. **Application Default Credentials via google-auth** — same ADC path as
   tier 1, reached only when GCE metadata is unavailable.

The fallback chain lets developers test BQ-touching codepaths
(``/api/v2/scan``, ``/api/v2/sample``, ``/api/v2/schema``, hybrid query)
on a Mac/Linux laptop without provisioning a GCE VM. Issue #112.

Tokens are cached in process memory until they expire (with a 60s safety
buffer), so consumers like ``src/db.py::_reattach_remote_extensions`` —
which runs on every readonly conn open — don't pay the round-trip cost
on every request.
"""

import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
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


def _fetch_adc_token() -> Tuple[str, int]:
    """Fall-back path using google-auth's Application Default Credentials.

    Covers the laptop path (`gcloud auth application-default login` ADC
    creds) and the `GOOGLE_APPLICATION_CREDENTIALS` service-account-key-file
    path. Returns ``(token, expires_in_seconds)``.

    Raises ``BQMetadataAuthError`` if google-auth isn't installed or no
    credentials are discoverable.
    """
    try:
        import google.auth  # noqa: PLC0415
        from google.auth.transport.requests import Request as _GAuthRequest
    except ImportError as e:
        raise BQMetadataAuthError("google-auth not installed; cannot fall back to ADC") from e
    try:
        creds, _project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    except Exception as e:  # google.auth.exceptions.DefaultCredentialsError
        raise BQMetadataAuthError(
            f"no Application Default Credentials found: {e}. "
            f"Run `gcloud auth application-default login` or set "
            f"GOOGLE_APPLICATION_CREDENTIALS to a service-account key file."
        ) from e
    try:
        creds.refresh(_GAuthRequest())
    except Exception as e:
        raise BQMetadataAuthError(f"ADC token refresh failed: {e}") from e
    if not creds.token:
        raise BQMetadataAuthError("ADC refresh returned no token")
    # google-auth gives an absolute expiry (datetime); compute seconds from now.
    import datetime as _dt

    if creds.expiry:
        # google-auth's `creds.expiry` is a naive datetime in UTC; compare
        # against the same wall clock without invoking the deprecated
        # `datetime.utcnow()`.
        now_utc = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        expires_in = max(0, int((creds.expiry - now_utc).total_seconds()))
    else:
        expires_in = 0
    return creds.token, expires_in


def _fetch_vault_sa_token() -> Tuple[str, int]:
    """Tier 2: vault ``BIGQUERY_SERVICE_ACCOUNT_JSON`` → in-memory credentials.

    Returns ``(access_token, expires_in_seconds)``.
    Raises ``BQMetadataAuthError`` if the vault entry is absent, the JSON is
    invalid, or google-auth is not installed.  The SA JSON is never written to
    disk — credentials are constructed in-memory only.
    """
    try:
        from app.datasource_secrets import datasource_secret  # noqa: PLC0415

        sa_json = datasource_secret("BIGQUERY_SERVICE_ACCOUNT_JSON")
    except Exception as e:
        raise BQMetadataAuthError(f"vault lookup failed: {e}") from e

    if not sa_json:
        raise BQMetadataAuthError("BIGQUERY_SERVICE_ACCOUNT_JSON not in vault")

    try:
        from google.oauth2 import service_account  # noqa: PLC0415
        from google.auth.transport.requests import Request as _GAuthRequest  # noqa: PLC0415
    except ImportError as e:
        raise BQMetadataAuthError("google-auth not installed; cannot use vault SA JSON") from e

    try:
        import json as _json  # noqa: PLC0415

        info = _json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/bigquery"]
        )
        creds.refresh(_GAuthRequest())
    except Exception as e:
        raise BQMetadataAuthError(f"vault SA credentials error: {e}") from e

    if not creds.token:
        raise BQMetadataAuthError("vault SA refresh returned no token")

    import datetime as _dt  # noqa: PLC0415

    if creds.expiry:
        now_utc = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        expires_in = max(0, int((creds.expiry - now_utc).total_seconds()))
    else:
        expires_in = 0
    return creds.token, expires_in


def get_metadata_token() -> str:
    """Return an OAuth access token suitable for BigQuery, cached in process.

    Resolution order (matches module-level docstring):
    1. ``GOOGLE_APPLICATION_CREDENTIALS`` env var set → ADC path honours it first.
    2. Vault ``BIGQUERY_SERVICE_ACCOUNT_JSON`` — in-memory, never written to disk.
    3. GCE metadata server — production fast-path.
    4. gcloud ADC / other ADC sources — laptop fallback.

    The function name keeps ``_metadata_`` for backwards compat — call sites
    don't need to know which path won.

    Returns the cached token when ``time.monotonic() < cached_expiry`` (with a
    60-second safety buffer applied at fetch time). Refreshes otherwise.

    Raises:
        BQMetadataAuthError: if all paths fail.
    """
    global _token_cache
    now = time.monotonic()

    with _cache_lock:
        cached = _token_cache
    if cached is not None and now < cached[1]:
        return cached[0]

    token: Optional[str] = None
    expires_in: int = 0

    # Tier 1: GOOGLE_APPLICATION_CREDENTIALS env var — explicit file-pinned SA key.
    # Check before vault so a deployment that sets GAC always uses that identity.
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        try:
            token, expires_in = _fetch_adc_token()
            logger.info("BQ auth: using GOOGLE_APPLICATION_CREDENTIALS")
        except BQMetadataAuthError:
            pass  # fall through to vault

    # Tier 2: vault SA JSON (in-memory; wins over ambient GCE identity).
    if not token:
        try:
            token, expires_in = _fetch_vault_sa_token()
            logger.info("BQ auth: using vault BIGQUERY_SERVICE_ACCOUNT_JSON")
        except BQMetadataAuthError:
            pass  # vault miss — try GCE metadata next

    if not token:
        # Tier 3: GCE metadata server — production fast path (no library import,
        # one HTTP call to a link-local address).
        metadata_err: Optional[BQMetadataAuthError] = None
        try:
            token, expires_in = _fetch_metadata_token()
        except BQMetadataAuthError as e:
            metadata_err = e
            # Tier 4: ADC (gcloud / other ADC sources without GAC env var).
            try:
                token, expires_in = _fetch_adc_token()
                logger.info(
                    "BQ auth: GCE metadata unreachable (%s); using ADC fallback",
                    e,
                )
            except BQMetadataAuthError as adc_err:
                # Surface BOTH error messages — operators on a laptop that
                # forgot `gcloud auth application-default login` should see
                # what to do next, not just the metadata-server failure.
                raise BQMetadataAuthError(f"GCE metadata: {metadata_err}; ADC fallback: {adc_err}") from adc_err

    # Only cache if expires_in is meaningfully large; if the response lacks the
    # field or returns 0/negative, skip caching so the next call retries.
    if token and expires_in > _CACHE_SAFETY_BUFFER_S:
        new_expiry = time.monotonic() + (expires_in - _CACHE_SAFETY_BUFFER_S)
        with _cache_lock:
            _token_cache = (token, new_expiry)
    return token  # type: ignore[return-value]
