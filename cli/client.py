"""HTTP client wrapper for CLI — handles auth, retries, streaming."""

import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from cli.config import _config_dir, get_server_url, get_token

# Retry policy for transient failures during stream downloads. Scoped to
# network issues and 5xx — 4xx (auth, 404, 400) is NOT retried. Tunable via
# env for tests; defaults sit in the "one flaky network blip" window.
_RETRY_ATTEMPTS = int(os.environ.get("AGNES_STREAM_RETRIES", "3"))
_RETRY_BACKOFFS_S = (0.3, 1.0, 3.0)  # seconds before attempt 2, 3, 4

# Long-running query timeout. /api/query forwards to BigQuery for remote
# tables, where SELECTs routinely run for minutes. The default 30s HTTP
# timeout dies long before BQ finishes. Operators tune via AGNES_QUERY_TIMEOUT.
QUERY_TIMEOUT_S = float(os.environ.get("AGNES_QUERY_TIMEOUT", "300"))


# ── Transport-error translation ─────────────────────────────────────────
# Pavel's Issue #185 Phase 3B caught the failure mode: when httpx raises
# `ReadTimeout` / `ConnectError` / `RemoteProtocolError` and the CLI
# command doesn't catch it, Typer dumps a five-frame Python traceback to
# the analyst's terminal. That looks like a CLI bug to a non-Python user
# and obscures the actionable signal ("server slow, try snapshot create").
# Translate transport exceptions to `AgnesTransportError` with a typed
# user-facing message, log the full traceback to `~/.config/agnes/last-
# error.log` for debug, and let the top-level CLI handler render the
# clean message + exit non-zero.

_LOG_FILE = _config_dir() / "last-error.log"


class AgnesTransportError(Exception):
    """Network / transport failure with a user-actionable message.

    Raised by the api_* / stream_download helpers when httpx surfaces a
    connection / timeout / protocol error. The CLI's top-level Typer
    handler catches this, prints `.user_message` (NOT the traceback),
    and exits non-zero. Full traceback goes to ``~/.config/agnes/last-
    error.log`` so an operator can recover it for support.
    """

    def __init__(self, user_message: str, *, hint: str = "", logfile_path: Path | None = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.hint = hint
        self.logfile_path = logfile_path


def _log_traceback(exc: BaseException, *, context: str) -> Path:
    """Append a timestamped traceback to ``~/.config/agnes/last-error.log``
    and return the path. Best-effort — never raises (a logging failure
    must not mask the original error)."""
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f"\n=== {ts} {context} ===\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass
    return _LOG_FILE


def _translate_transport_error(exc: Exception, *, context: str) -> AgnesTransportError:
    """Map httpx transport exceptions to user-facing CLI messages. The
    mapping is intentionally pragmatic — analysts care about "what do I
    do next", not the gRPC / TCP detail."""
    log = _log_traceback(exc, context=context)
    if isinstance(exc, httpx.ReadTimeout):
        return AgnesTransportError(
            f"Server didn't respond within the read timeout ({QUERY_TIMEOUT_S:.0f}s) "
            f"for {context}.",
            hint=(
                "If this is `agnes query --remote` against a heavy BQ view, "
                "the underlying BQ job took longer than the wait window. Try:\n"
                "  • narrow the WHERE (especially the partition column from `agnes catalog --json`)\n"
                "  • `agnes snapshot create <table> ... --estimate` to materialize once + query locally\n"
                "  • set AGNES_QUERY_TIMEOUT=600 for a longer client-side wait\n"
                f"Full traceback: {log}"
            ),
            logfile_path=log,
        )
    if isinstance(exc, httpx.ConnectError):
        return AgnesTransportError(
            f"Can't reach the agnes server for {context}.",
            hint=(
                "Check the server URL with `agnes status`, network reachability "
                "(VPN / DNS / firewall), and the TLS-trust setup if this is a "
                f"corporate-CA deployment.\nFull traceback: {log}"
            ),
            logfile_path=log,
        )
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
        return AgnesTransportError(
            f"Connection broke mid-flight on {context}.",
            hint=(
                "Usually a transient network blip. Re-run the command. If it "
                f"keeps happening, check `agnes status`.\nFull traceback: {log}"
            ),
            logfile_path=log,
        )
    if isinstance(exc, httpx.TimeoutException):
        return AgnesTransportError(
            f"Network timeout on {context}.",
            hint=f"Re-run; if persistent, check the server.\nFull traceback: {log}",
            logfile_path=log,
        )
    # Anything else: re-wrap with a generic message so the CLI doesn't
    # dump the traceback. We'd prefer a typed translation; if you hit
    # this branch, add a clause above.
    return AgnesTransportError(
        f"Unexpected error on {context}: {type(exc).__name__}.",
        hint=f"Full traceback: {log}",
        logfile_path=log,
    )


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


def api_get(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.get(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"GET {path}") from exc


def api_post(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.post(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"POST {path}") from exc


def api_delete(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.delete(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"DELETE {path}") from exc


def api_patch(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.patch(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"PATCH {path}") from exc


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
    # Clean up any leftover tmp, then surface the last exception. Translate
    # transport errors (timeouts, connection drops, protocol errors) to
    # AgnesTransportError so the CLI prints a clean message instead of a
    # Python traceback (Pavel's #185 Phase 3B). HTTPStatusError (4xx/5xx
    # response from the server) is NOT a transport failure and must
    # re-raise verbatim so the caller's status-code handling + the rich
    # server error body (e.g. 401 with "token expired", 403 with
    # cross_project_forbidden detail) reach the analyst — Devin Review on
    # PR #188 caught: HTTPStatusError is a subclass of HTTPError, so the
    # generic isinstance(HTTPError) translation was eating status codes.
    tmp_path.unlink(missing_ok=True)
    assert last_exc is not None
    if isinstance(last_exc, httpx.HTTPStatusError):
        raise last_exc
    if isinstance(last_exc, httpx.HTTPError):
        raise _translate_transport_error(
            last_exc, context=f"GET {path} (stream → {target_path})"
        ) from last_exc
    raise last_exc
