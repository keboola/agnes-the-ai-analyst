"""HTTP client wrapper for CLI — handles auth, retries, streaming."""

import os
import time
from pathlib import Path
from typing import Optional

import httpx

from cli.config import get_server_url, get_token

# Retry policy for transient failures during stream downloads. Scoped to
# network issues and 5xx — 4xx (auth, 404, 400) is NOT retried. Tunable via
# env for tests; defaults sit in the "one flaky network blip" window.
_RETRY_ATTEMPTS = int(os.environ.get("DA_STREAM_RETRIES", "3"))
_RETRY_BACKOFFS_S = (0.3, 1.0, 3.0)  # seconds before attempt 2, 3, 4


def get_client(timeout: float = 30.0) -> httpx.Client:
    """Get an authenticated httpx client."""
    token = get_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=get_server_url(),
        headers=headers,
        timeout=timeout,
    )


def api_get(path: str, **kwargs) -> httpx.Response:
    with get_client() as client:
        return client.get(path, **kwargs)


def api_post(path: str, **kwargs) -> httpx.Response:
    with get_client() as client:
        return client.post(path, **kwargs)


def api_delete(path: str, **kwargs) -> httpx.Response:
    with get_client() as client:
        return client.delete(path, **kwargs)


def api_patch(path: str, **kwargs) -> httpx.Response:
    with get_client() as client:
        return client.patch(path, **kwargs)


def _is_transient(exc: Exception) -> bool:
    """Worth retrying? Network blip or 5xx — yes. Auth / 4xx — no."""
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.WriteError,
                        httpx.RemoteProtocolError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def stream_download(path: str, target_path: str, progress_callback=None) -> int:
    """Stream a file to `target_path` atomically and with retries.

    Durability properties:
    - Writes to `target_path + ".tmp"`, then `os.replace` on success. The
      real target file never exists in a half-written state.
    - Retries up to `_RETRY_ATTEMPTS` times on transient errors (network
      blip, 5xx); 4xx (auth/404) is raised immediately.
    - No hash check here — that's done in the sync command against the
      manifest hash, because only the caller knows the expected value.
    """
    tmp_path = Path(f"{target_path}.tmp")
    last_exc: Optional[Exception] = None
    for attempt in range(_RETRY_ATTEMPTS + 1):
        try:
            tmp_path.unlink(missing_ok=True)
            with get_client(timeout=300.0) as client:
                with client.stream("GET", path) as response:
                    response.raise_for_status()
                    total = 0
                    with open(tmp_path, "wb") as f:
                        for chunk in response.iter_bytes(chunk_size=65536):
                            f.write(chunk)
                            total += len(chunk)
                            if progress_callback:
                                progress_callback(len(chunk))
            # os.replace is atomic on POSIX and Windows for same-filesystem moves.
            os.replace(tmp_path, target_path)
            return total
        except Exception as exc:
            last_exc = exc
            if attempt == _RETRY_ATTEMPTS or not _is_transient(exc):
                break
            time.sleep(_RETRY_BACKOFFS_S[min(attempt, len(_RETRY_BACKOFFS_S) - 1)])
    # Clean up any leftover tmp, then surface the last exception.
    tmp_path.unlink(missing_ok=True)
    assert last_exc is not None
    raise last_exc
