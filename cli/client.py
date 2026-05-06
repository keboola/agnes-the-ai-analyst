"""HTTP client wrapper for CLI — handles auth, retries, streaming."""

import atexit
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Range-chunked parallel download — see `stream_download` docstring. Defaults
# tuned for the corp-VPN per-flow rate-limiting case (single-stream throttled
# but N parallel range requests scale linearly). Disabled implicitly for
# files below the threshold or when the server doesn't advertise byte-range
# support. Operators can hard-disable by setting parallelism to 1.
_CHUNK_PARALLELISM = max(1, min(16, int(
    os.environ.get("AGNES_PULL_CHUNK_PARALLELISM", "4"),
)))
_CHUNK_THRESHOLD_BYTES = int(
    os.environ.get("AGNES_PULL_CHUNK_THRESHOLD_BYTES", str(50 * 1024 * 1024)),
)


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


def _translate_transport_error(
    exc: Exception, *, context: str, timeout_s: float | None = None,
) -> AgnesTransportError:
    """Map httpx transport exceptions to user-facing CLI messages. The
    mapping is intentionally pragmatic — analysts care about "what do I
    do next", not the gRPC / TCP detail.

    `timeout_s`, when supplied, is the actual httpx timeout used by the
    failing call so the ReadTimeout message reports the real wait window
    (a `agnes catalog` GET dies at 30s, not 300s — Devin Review on PR
    #188 caught the original signature hardcoding `QUERY_TIMEOUT_S`,
    which only matches `agnes query --remote`)."""
    log = _log_traceback(exc, context=context)
    if isinstance(exc, httpx.ReadTimeout):
        wait_s = timeout_s if timeout_s is not None else QUERY_TIMEOUT_S
        # The "long-running BQ" advisory only makes sense when the call
        # actually hit the query path (timeout ≥ ~60s). For short calls
        # (the 30s default on `agnes catalog` etc.) it's just confusing.
        if wait_s >= 60:
            hint = (
                "If this is `agnes query --remote` against a heavy BQ view, "
                "the underlying BQ job took longer than the wait window. Try:\n"
                "  • narrow the WHERE (especially the partition column from `agnes catalog --json`)\n"
                "  • `agnes snapshot create <table> ... --estimate` to materialize once + query locally\n"
                "  • set AGNES_QUERY_TIMEOUT=600 for a longer client-side wait\n"
                f"Full traceback: {log}"
            )
        else:
            hint = (
                "Server is slow or unreachable. Check `agnes status`; "
                "re-run if transient.\n"
                f"Full traceback: {log}"
            )
        return AgnesTransportError(
            f"Server didn't respond within the read timeout ({wait_s:.0f}s) "
            f"for {context}.",
            hint=hint,
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
    """Get an authenticated httpx client.

    This factory creates a fresh client per call — used by the small
    `api_*` helpers (one request, then close). The big-stream path
    (`stream_download`) routes through `_get_shared_client()` to amortize
    TLS handshakes and HTTP/2 multiplexing across N parquet downloads.
    """
    token = get_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=get_server_url(),
        headers=headers,
        timeout=timeout,
    )


# ── Shared persistent client ────────────────────────────────────────────
# `agnes pull` issues N stream_download calls — one per parquet — plus
# (with chunked downloads) M Range requests per file. Without pooling,
# each call performs a fresh TLS handshake; with HTTP/2 enabled, all
# those requests multiplex over a single TCP connection. The shared
# client is created lazily on first stream-download request, kept alive
# for the duration of the process, and closed at exit.
#
# HTTP/2 requires the optional `h2` package. If it's unavailable (slim
# install), we fall back to HTTP/1.1 — pooling alone still saves the
# handshake cost — and never raise. The CLI must not crash on `agnes
# pull` because of an h2 import error.

_SHARED_CLIENT: Optional[httpx.Client] = None
_SHARED_CLIENT_LOCK = threading.Lock()


def _get_shared_client() -> httpx.Client:
    """Lazily create + return a process-wide httpx.Client.

    Pool defaults: keep up to 32 keepalive connections (covers the
    chunk-parallelism cap of 16 × 2 simultaneous files comfortably) and
    cap the total at 64 so a runaway loop can't open thousands of
    sockets. HTTP/2 is opt-in via httpx's `http2=True` and gracefully
    degrades when the `h2` extra is missing.
    """
    global _SHARED_CLIENT
    with _SHARED_CLIENT_LOCK:
        if _SHARED_CLIENT is not None:
            return _SHARED_CLIENT
        token = get_token()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        limits = httpx.Limits(
            max_keepalive_connections=32,
            max_connections=64,
        )
        try:
            client = httpx.Client(
                base_url=get_server_url(),
                headers=headers,
                timeout=300.0,
                http2=True,
                limits=limits,
            )
        except (ImportError, RuntimeError):
            # `h2` not installed → httpx raises; fall back to HTTP/1.1.
            # Pooling alone still amortizes the TLS handshake.
            client = httpx.Client(
                base_url=get_server_url(),
                headers=headers,
                timeout=300.0,
                limits=limits,
            )
        _SHARED_CLIENT = client
        return client


def _close_shared_client() -> None:
    """Close the shared client and clear the slot. Safe to call twice."""
    global _SHARED_CLIENT
    with _SHARED_CLIENT_LOCK:
        if _SHARED_CLIENT is not None:
            try:
                _SHARED_CLIENT.close()
            except Exception:
                pass
            _SHARED_CLIENT = None


atexit.register(_close_shared_client)


def api_get(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.get(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"GET {path}", timeout_s=timeout) from exc


def api_post(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.post(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"POST {path}", timeout_s=timeout) from exc


def api_delete(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.delete(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"DELETE {path}", timeout_s=timeout) from exc


def api_patch(path: str, *, timeout: float = 30.0, **kwargs) -> httpx.Response:
    try:
        with get_client(timeout=timeout) as client:
            return client.patch(path, **kwargs)
    except httpx.HTTPError as exc:
        raise _translate_transport_error(exc, context=f"PATCH {path}", timeout_s=timeout) from exc


def _is_transient(exc: Exception) -> bool:
    """Worth retrying? Network blip or 5xx — yes. Auth / 4xx — no."""
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.WriteError,
                        httpx.RemoteProtocolError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _read_chunk_threshold_bytes() -> int:
    """Re-read threshold each call so tests / operators can flip it via
    env var without restarting the process."""
    try:
        return int(os.environ.get(
            "AGNES_PULL_CHUNK_THRESHOLD_BYTES", str(_CHUNK_THRESHOLD_BYTES),
        ))
    except ValueError:
        return _CHUNK_THRESHOLD_BYTES


def _read_chunk_parallelism() -> int:
    """Re-read parallelism each call (same rationale as threshold). Floor 1,
    ceiling 16."""
    try:
        n = int(os.environ.get(
            "AGNES_PULL_CHUNK_PARALLELISM", str(_CHUNK_PARALLELISM),
        ))
    except ValueError:
        n = _CHUNK_PARALLELISM
    return max(1, min(16, n))


def _probe_range_support(client: httpx.Client, path: str) -> tuple[int, bool]:
    """Send HEAD; return (content-length, accepts-byte-ranges).

    `(0, False)` means "we couldn't tell — fall back to single-stream".
    Never raises; transport errors during the probe are treated as
    "no chunking, try the GET instead and let it surface the failure
    in the normal retry loop".

    Probe order: HEAD first (cheap, idempotent), then GET-with-tiny-range
    fallback. The HEAD path covers Caddy's `file_server` (which advertises
    HEAD) and Caddy's `reverse_proxy` (which forwards HEAD upstream). The
    GET-fallback covers the dev `docker compose up` deployment where
    requests go straight to FastAPI's GET-only `/api/data/{tid}/download`
    route — FastAPI returns **405 Method Not Allowed** to a HEAD on a
    GET-only route, which without this fallback would silently disable
    chunked download for every dev / non-TLS install. The GET-with-Range
    probe asks for 1 byte so the server response is bounded; we discard
    the body and read only the headers + status code.
    """
    try:
        resp = client.head(path)
        status = getattr(resp, "status_code", 200)
        if status < 400:
            size = int(resp.headers.get("content-length", "0") or 0)
            accepts = (resp.headers.get("accept-ranges", "").lower() == "bytes")
            if size > 0:
                return (size, accepts)
        # HEAD failed (405 from GET-only route is the common case in
        # non-Caddy deployments) or returned 0-length — fall through to
        # the tiny-Range GET probe.
    except Exception:
        pass
    try:
        with client.stream("GET", path, headers={"Range": "bytes=0-0"}) as resp:
            status = getattr(resp, "status_code", 0)
            if status not in (200, 206):
                return (0, False)
            # Drain the 1-byte body so the connection is reusable.
            for _ in resp.iter_bytes():
                pass
            # Content-Range on a 206 response carries the total: `bytes 0-0/12345`.
            # On a 200 response the server didn't honor Range — content-length is the total.
            if status == 206:
                cr = resp.headers.get("content-range", "")
                if "/" in cr:
                    try:
                        total = int(cr.rsplit("/", 1)[1])
                        return (total, True)
                    except ValueError:
                        return (0, False)
                return (0, False)
            # status == 200 → server ignored Range; we can read content-length but
            # accept-ranges is False (or missing) so the caller will not chunk.
            size = int(resp.headers.get("content-length", "0") or 0)
            accepts = (resp.headers.get("accept-ranges", "").lower() == "bytes")
            return (size, accepts)
    except Exception:
        return (0, False)


class _RangeNotHonored(Exception):
    """Internal sentinel — server returned 200 instead of 206 to a Range
    request. Caller catches and falls back to the single-stream path."""


def _download_chunk(
    client: httpx.Client,
    path: str,
    start: int,
    end: int,
    part_path: Path,
    progress_callback,
) -> None:
    """Stream `bytes=start-end` to `part_path`. Caller deals with retry +
    cleanup. Raises on any failure (HTTPStatusError on non-206 response,
    httpx.* on transport blip, `_RangeNotHonored` if server returned 200
    instead of 206 — chunked path can't trust that result)."""
    headers = {"Range": f"bytes={start}-{end}"}
    with client.stream("GET", path, headers=headers) as response:
        # Server didn't honor the Range — RFC says it MAY return 200 with
        # the full body. We can't safely splice that into one part of N,
        # so we abort the whole chunked path and let the caller fall back.
        if response.status_code == 200:
            raise _RangeNotHonored()
        response.raise_for_status()
        with open(part_path, "wb") as f:
            for piece in response.iter_bytes(chunk_size=65536):
                f.write(piece)
                if progress_callback and piece:
                    progress_callback(len(piece))


def _download_chunked(
    client: httpx.Client,
    path: str,
    target_path: str,
    total_size: int,
    parallelism: int,
    progress_callback,
) -> int:
    """Range-based parallel download. Returns total bytes written.

    Raises `_RangeNotHonored` on the first 200-instead-of-206 response so
    the caller can fall back. All other exceptions propagate.

    Cleanup discipline: every part file we create gets removed before
    return (success or failure). The destination is written via the
    caller's `<target>.tmp` and renamed atomically.
    """
    target = Path(target_path)
    # Per-process tmp + part suffixes (devil's-advocate R2 finding #2):
    # if two `agnes pull` invocations target the same parquet
    # concurrently (e.g. SessionStart hook + manual run, or two
    # terminals), bare `<target>.tmp` and `<target>.partN` paths would
    # collide — one process's part-write yanks the other's in-progress
    # write, manifest hash check then fails spuriously. Including PID
    # in the suffix makes each invocation's intermediate files
    # disjoint; the final `os.replace` to the bare target is atomic so
    # last-writer-wins, both processes succeed individually.
    pid = os.getpid()
    tmp_path = Path(f"{target_path}.{pid}.tmp")
    parallelism = max(1, parallelism)
    # Build chunks — last chunk takes the remainder.
    chunk_size = total_size // parallelism
    if chunk_size <= 0:
        chunk_size = total_size  # tiny file, single chunk
        parallelism = 1
    ranges = []
    for i in range(parallelism):
        start = i * chunk_size
        end = (start + chunk_size - 1) if i < parallelism - 1 else (total_size - 1)
        ranges.append((i, start, end))

    part_paths = [Path(f"{target_path}.{pid}.part{i}") for i, _, _ in ranges]
    # Pre-clean any leftovers from a prior run of THIS process.
    for p in part_paths:
        p.unlink(missing_ok=True)

    def _attempt_chunk(i: int, start: int, end: int) -> None:
        last_exc: Optional[Exception] = None
        for attempt in range(_RETRY_ATTEMPTS + 1):
            try:
                _download_chunk(
                    client, path, start, end, part_paths[i],
                    progress_callback,
                )
                return
            except _RangeNotHonored:
                # Don't retry — server policy, not a transport blip.
                raise
            except Exception as exc:
                last_exc = exc
                if attempt == _RETRY_ATTEMPTS or not _is_transient(exc):
                    break
                time.sleep(_RETRY_BACKOFFS_S[
                    min(attempt, len(_RETRY_BACKOFFS_S) - 1)
                ])
        assert last_exc is not None
        raise last_exc

    try:
        if parallelism == 1:
            _attempt_chunk(*ranges[0])
        else:
            # Use a thread pool so each chunk gets its own concurrent
            # request slot on the (HTTP/2-multiplexed when available)
            # shared client. httpx.Client is thread-safe for stream().
            with ThreadPoolExecutor(max_workers=parallelism) as ex:
                futs = [ex.submit(_attempt_chunk, *r) for r in ranges]
                for fut in as_completed(futs):
                    fut.result()  # propagate first error

        # Concatenate parts → tmp_path → atomic rename.
        tmp_path.unlink(missing_ok=True)
        total_written = 0
        with open(tmp_path, "wb") as out:
            for p in part_paths:
                with open(p, "rb") as inp:
                    while True:
                        block = inp.read(65536)
                        if not block:
                            break
                        out.write(block)
                        total_written += len(block)
        os.replace(tmp_path, target)
        return total_written
    finally:
        # Always clean up part files + any stray tmp.
        for p in part_paths:
            p.unlink(missing_ok=True)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _download_single_stream(
    client: httpx.Client,
    path: str,
    target_path: str,
    progress_callback,
) -> int:
    """Original single-stream path with retry. Used when chunking is
    disabled (small file, no range support, or fallback after 200-on-Range)."""
    # Per-process tmp suffix — same rationale as `_download_chunked`
    # (devil's-advocate R2 finding #2): concurrent `agnes pull`
    # invocations against the same target dir must not yank each
    # other's in-progress writes.
    tmp_path = Path(f"{target_path}.{os.getpid()}.tmp")
    last_exc: Optional[Exception] = None
    for attempt in range(_RETRY_ATTEMPTS + 1):
        try:
            tmp_path.unlink(missing_ok=True)
            with client.stream("GET", path) as response:
                response.raise_for_status()
                total = 0
                with open(tmp_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=65536):
                        f.write(chunk)
                        total += len(chunk)
                        if progress_callback:
                            progress_callback(len(chunk))
            os.replace(tmp_path, target_path)
            return total
        except Exception as exc:
            last_exc = exc
            if attempt == _RETRY_ATTEMPTS or not _is_transient(exc):
                break
            time.sleep(_RETRY_BACKOFFS_S[min(attempt, len(_RETRY_BACKOFFS_S) - 1)])
    tmp_path.unlink(missing_ok=True)
    assert last_exc is not None
    raise last_exc


def stream_download(path: str, target_path: str, progress_callback=None) -> int:
    """Stream a file to `target_path` atomically and with retries.

    Two paths:
    1. **Chunked parallel** — when the server advertises `accept-ranges:
       bytes` and `content-length` exceeds `AGNES_PULL_CHUNK_THRESHOLD_BYTES`
       (default 50 MB), split into N range requests
       (`AGNES_PULL_CHUNK_PARALLELISM`, default 4, capped 1..16) and
       download in parallel. Concatenate the part files into `<target>.tmp`,
       then `os.replace`. Falls back to single-stream if the server
       responds 200 instead of 206 to a Range probe.
    2. **Single-stream** — for small files, no range support, or fallback
       from the chunked path. Same atomic-rename + retry semantics as
       before.

    Durability properties (unchanged):
    - Writes to `<target>.tmp`, then `os.replace` on success. The real
      target file never exists in a half-written state.
    - Retries up to `_RETRY_ATTEMPTS` on transient errors (network blip,
      5xx); 4xx (auth/404) is raised immediately.
    - No hash check here — that's the caller's job (manifest hash).

    Threading: the chunked path uses a ThreadPoolExecutor sized to the
    parallelism. httpx.Client.stream() is safe to call concurrently from
    multiple threads on a single client (the connection pool serializes
    the underlying socket access; HTTP/2 multiplexes streams when the
    `h2` extra is installed).
    """
    # Use the shared persistent client when available — one TLS
    # handshake amortized across N stream_download calls within the same
    # process, and HTTP/2 stream multiplexing across the chunk Range
    # requests within a single download. Falls back to a fresh per-call
    # client if shared-client construction fails (e.g. `h2` install
    # broken at runtime). Devil's-advocate R2 finding #1: scope the
    # try/except to *only* the shared-client construction — the actual
    # download must NOT be retried under this except, otherwise hard
    # failures (401/403/404/5xx) waste a full second download attempt
    # and revoked-PAT cases don't fail-fast.
    try:
        client = _get_shared_client()
    except Exception:
        with get_client(timeout=300.0) as client:
            return _stream_download_via(client, path, target_path, progress_callback)
    return _stream_download_via(client, path, target_path, progress_callback)


def _stream_download_via(
    client: httpx.Client,
    path: str,
    target_path: str,
    progress_callback,
) -> int:
    """The shared body of `stream_download` parameterized on the client.
    Split out so tests can inject a fake client."""
    threshold = _read_chunk_threshold_bytes()
    parallelism = _read_chunk_parallelism()

    total_size = 0
    accepts_ranges = False
    if parallelism > 1:
        total_size, accepts_ranges = _probe_range_support(client, path)

    # Sanity bound on the advertised total size (devil's-advocate R1
    # finding #4): a misconfigured proxy or buggy server returning a
    # wildly inflated `Content-Length` would make us split into huge
    # `Range: bytes=N-M` requests; the server then clamps each to actual
    # bytes available, and we end up with overlapping bytes from the
    # start of the file in every part → corrupt assembled output (caught
    # later by manifest hash check, but only after wasted bandwidth).
    # 100 GiB is the operational ceiling for any single materialized
    # parquet on a typical Agnes deployment; values above suggest a
    # server / proxy bug rather than a legitimate huge file. Drop to
    # single-stream (which can't be confused by overlapping chunks).
    SANE_MAX_TOTAL = 100 * 1024**3  # 100 GiB
    if total_size > SANE_MAX_TOTAL:
        total_size = 0
        accepts_ranges = False

    use_chunked = (
        parallelism > 1
        and accepts_ranges
        and total_size > threshold
    )

    try:
        if use_chunked:
            try:
                return _download_chunked(
                    client, path, target_path, total_size, parallelism,
                    progress_callback,
                )
            except _RangeNotHonored:
                # Server lied / proxy stripped the Range — fall through.
                pass
        return _download_single_stream(
            client, path, target_path, progress_callback,
        )
    except httpx.HTTPStatusError:
        # 4xx / 5xx response from the server — re-raise verbatim so the
        # caller's status-code handling + the rich server error body
        # reach the analyst (Devin Review on PR #188).
        raise
    except httpx.HTTPError as exc:
        raise _translate_transport_error(
            exc, context=f"GET {path} (stream → {target_path})",
            timeout_s=300.0,
        ) from exc
