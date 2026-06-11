"""`run_pull` — pure data-refresh primitive lifted from `cli/commands/sync.py`.

Pulls the RBAC-filtered manifest from the server, downloads parquets whose
MD5 hash differs from local state, rebuilds DuckDB views, and syncs the
corporate memory bundle to `<workspace>/.claude/rules/km_*.md`.

Contract — Task 8:
- Pure function: no Typer, no stdout, no `sys.exit`. Caller decides what to print.
- Returns a `PullResult` dataclass.
- `dry_run=True` -> no disk writes anywhere (no DB file, no parquet dir,
  no rules dir, no sync_state).
- Lazy mkdir: `server/parquet/` is created inside the per-table loop on
  first write; `.claude/rules/` is only created when the bundle has at
  least one mandatory item or non-empty approved list. Empty inputs leave
  the workspace tree alone.
- The DuckDB file at `<workspace>/user/duckdb/analytics.duckdb` is the
  load-bearing artifact for every downstream reader (CLI query, hooks),
  so it gets created even with zero parquets.

The api_get/stream_download helpers in `cli/client.py` read server URL and
token from `cli.config` (via the `AGNES_SERVER` and `AGNES_TOKEN` env
overrides). To keep `run_pull` callable with explicit `server_url` /
`token` arguments without rewriting the HTTP layer, this module sets those
env vars for the duration of the call and restores the prior values on
exit. That's the cheapest adapter that doesn't bleed into client.py.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from cli.client import api_get, api_post, stream_download
from cli.config import get_sync_state, save_sync_state


@dataclass
class PullResult:
    """Outcome of a `run_pull` invocation.

    Fields:
    - `tables_updated`: count of parquets actually re-downloaded this run.
    - `tables_removed`: count of local `server/parquet/<name>.parquet` files
      pruned this run because the table left the authorized typed (v49)
      stack. Always 0 against a pre-v49 server that emits no typed sections.
    - `parquets_total`: count of non-remote tables visible in the manifest.
    - `rules_count`: number of `km_*.md` files written to `.claude/rules/`.
    - `duration_s`: wall time of the call.
    - `errors`: list of `{"table": ..., "error": ...}` (or
      `{"stage": "memory_bundle", "error": ...}`) — best-effort flow,
      individual failures don't abort the whole pull.
    """

    tables_updated: int = 0
    tables_removed: int = 0
    parquets_total: int = 0
    rules_count: int = 0
    duration_s: float = 0.0
    errors: list[dict] = field(default_factory=list)
    # v49 (Phase 7, Task 7.5) — per-type stack-sync result. Populated when
    # the manifest carries any of ``direct_tables`` / ``data_packages`` /
    # ``memory_domains``. Kept off the constructor signature (None default)
    # so older callers reading ``tables_updated`` keep compiling.
    stack_sync: object = None


_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")

# #596 — hash-mismatch recovery in `_download_one`. A download whose bytes
# don't match the manifest hash is treated as transient (corrupt mid-flight
# transfer, a server-side parquet rewrite that raced the manifest read) and
# re-downloaded up to this many extra times before the table is recorded as
# a hard error. The prior good `<tid>.parquet` is preserved across the whole
# loop (download lands in a sidecar; only a verified sidecar is promoted), so
# even a persistent mismatch never leaves the table missing from disk.
_DOWNLOAD_RETRIES = 2
_DOWNLOAD_RETRY_BACKOFFS_S = (0.5, 1.0)


def _read_progress_interval_seconds() -> float:
    """Seconds between forced progress emissions per file. Default 5 s.

    Tighter cadence than the original 30 s default keeps non-TTY consumers
    (Claude Code sub-agent watchdogs, CI runners) from killing the process
    on apparent silence during a slow chunk. Override via
    `AGNES_PULL_PROGRESS_INTERVAL_SECONDS`. Issue #203.
    """
    raw = os.environ.get("AGNES_PULL_PROGRESS_INTERVAL_SECONDS", "")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 5.0


def _read_progress_interval_bytes() -> int:
    """Bytes between forced progress emissions per file. Default 1 MiB.

    Complements the time-based cadence so fast downloads also emit at a
    reasonable rate (the original "every 10% of total" boundary went
    unobserved on multi-GB parquets where 10% is tens of seconds of bytes).
    Override via `AGNES_PULL_PROGRESS_INTERVAL_BYTES`. Issue #203.
    """
    raw = os.environ.get("AGNES_PULL_PROGRESS_INTERVAL_BYTES", "")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 1024 * 1024


class _TextualProgress:
    """Plain-text progress emitter for non-TTY stderr.

    When `agnes pull` is invoked from a Claude Code SessionStart hook,
    a CI runner, or any pipe consumer, stderr is not a terminal. Rich's
    progress bar in that mode either suppresses output (silent for
    minutes on a multi-GB parquet) or emits raw ANSI noise. This class
    instead emits one terse line per file at sensible cadence.

    Cadence policy: emit when *any* of:
      - per-file bytes-downloaded crosses a 10%-of-total boundary, OR
      - more than ``AGNES_PULL_PROGRESS_INTERVAL_BYTES`` bytes (default
        1 MiB) since this file's last emission, OR
      - more than ``AGNES_PULL_PROGRESS_INTERVAL_SECONDS`` (default 5 s)
        since this file's last emission.

    The byte+second floor exists because sub-agent / CI watchdogs read
    "no output for N seconds" as a hung process and kill it (issue #203);
    the original 30 s / 10% policy was silent enough to trip those gates
    on slow links.

    Always emits one final "done" line per file via `finish()` so the
    operator sees a confirmed completion even on tiny files.

    Format: `[N/T files] <tid>: 25% (16 MB / 66 MB) at 1.5 MB/s` — the
    "[N/T files]" prefix lets the operator see overall pull progress
    in a multi-table run without buffering all per-file lines.

    Thread-safe — `advance` is called from the chunked-download worker
    threads; an internal lock serializes the update + emit.
    """

    _HUMAN_UNITS = (
        (1024 * 1024 * 1024 * 1024, "TB"),
        (1024 * 1024 * 1024, "GB"),
        (1024 * 1024, "MB"),
        (1024, "KB"),
    )

    def __init__(self, *, stream, total_files: int, file_sizes: dict[str, int]):
        import threading
        self._stream = stream
        self._total_files = total_files
        self._file_sizes = file_sizes
        self._lock = threading.Lock()
        self._interval_seconds = _read_progress_interval_seconds()
        self._interval_bytes = _read_progress_interval_bytes()
        # Per-file state.
        self._bytes: dict[str, int] = {tid: 0 for tid in file_sizes}
        self._started_at: dict[str, float] = {}
        self._last_emit_at: dict[str, float] = {}
        self._last_emit_pct: dict[str, int] = {}
        self._last_emit_bytes: dict[str, int] = {}
        self._finished_idx: int = 0  # files whose `finish` line has been emitted

    def advance(self, tid: str, n: int) -> None:
        """Add `n` bytes to the file's total. Emit a textual update if
        the cadence policy allows."""
        with self._lock:
            now = time.monotonic()
            if tid not in self._started_at:
                self._started_at[tid] = now
                self._last_emit_at[tid] = now
                self._last_emit_pct[tid] = 0
                self._last_emit_bytes[tid] = 0
            self._bytes[tid] = self._bytes.get(tid, 0) + n

            total = self._file_sizes.get(tid, 0)
            current = self._bytes[tid]
            pct = int((current * 100) / total) if total > 0 else 0
            elapsed = now - self._last_emit_at[tid]
            bytes_since_emit = current - self._last_emit_bytes.get(tid, 0)
            crossed_10 = pct >= self._last_emit_pct[tid] + 10
            if (
                crossed_10
                or elapsed >= self._interval_seconds
                or bytes_since_emit >= self._interval_bytes
            ):
                self._last_emit_at[tid] = now
                self._last_emit_pct[tid] = pct - (pct % 10)
                self._last_emit_bytes[tid] = current
                self._emit_line(tid, current, total, now)

    def finish(self) -> None:
        """Emit a final `done` line for any file we never closed out."""
        with self._lock:
            now = time.monotonic()
            for tid, total in self._file_sizes.items():
                # Treat any file we observed bytes for as needing a
                # final line. Files that errored out before any callback
                # are still announced (operator wants visibility even on
                # zero-byte attempts).
                self._finished_idx += 1
                bytes_ = self._bytes.get(tid, 0)
                started = self._started_at.get(tid, now)
                duration = max(0.001, now - started)
                rate = bytes_ / duration
                line = (
                    f"[{self._finished_idx}/{self._total_files} files] "
                    f"{tid}: 100% done "
                    f"({self._fmt_bytes(bytes_)} in {duration:.1f}s, "
                    f"{self._fmt_bytes(int(rate))}/s)\n"
                )
                self._stream.write(line)
            try:
                self._stream.flush()
            except Exception:
                pass

    def _emit_line(self, tid: str, current: int, total: int, now: float) -> None:
        started = self._started_at.get(tid, now)
        duration = max(0.001, now - started)
        rate = current / duration
        if total > 0:
            # Clamp displayed percentage to [0, 100]. When `current`
            # exceeds the advertised `total` (range/chunked transfer
            # over-counts, manifest size is compressed vs response is
            # decompressed, server retransmits a chunk, etc.) the raw
            # percentage would creep past 100% and snap back at
            # `finish()`, which surfaced in 2026-05-12 sub-agent perf
            # tests as confusing "174%" lines. Issue #258.
            raw_pct = int((current * 100) / total)
            pct_display = min(raw_pct, 100)
            pct_str = f"{pct_display}%"
            size_str = (
                f"({self._fmt_bytes(current)} / {self._fmt_bytes(total)})"
            )
        else:
            pct_str = "?"
            size_str = f"({self._fmt_bytes(current)})"
        idx = self._finished_idx + 1  # 1-based "currently working on file N"
        line = (
            f"[{idx}/{self._total_files} files] {tid}: {pct_str} "
            f"{size_str} at {self._fmt_bytes(int(rate))}/s\n"
        )
        self._stream.write(line)
        try:
            self._stream.flush()
        except Exception:
            pass

    @classmethod
    def _fmt_bytes(cls, n: int) -> str:
        for divisor, suffix in cls._HUMAN_UNITS:
            if n >= divisor:
                return f"{n / divisor:.1f} {suffix}"
        return f"{n} B"


@contextmanager
def _override_server_env(server_url: str, token: str) -> Iterator[None]:
    """Set AGNES_SERVER + scoped token override for the duration of the call.

    `cli.config.get_server_url` honors `AGNES_SERVER`, so the server URL is
    swapped via env-var. The TOKEN override is routed through
    `cli.config._with_token_override` (a ContextVar), which is checked by
    `get_token()` BEFORE the on-disk `~/.config/agnes/token.json`. This is
    load-bearing: `agnes init --token NEW` runs the verify call in step 2
    while the file still holds an OLD token from a prior install — without
    the override, the verify uses the stale on-disk token and fails 401.

    `AGNES_TOKEN` env var is also set as a back-compat hint for any code
    path that bypasses `get_token()` (none in `cli/` at last audit, but
    third-party hooks may), but the contextvar is the authoritative source.

    Restores prior values on exit so the caller's environment isn't
    mutated permanently. Not safe for concurrent invocation across threads;
    single-threaded use only.
    """
    from cli.config import _with_token_override

    prev_server = os.environ.get("AGNES_SERVER")
    prev_token = os.environ.get("AGNES_TOKEN")
    os.environ["AGNES_SERVER"] = server_url
    if token:
        os.environ["AGNES_TOKEN"] = token
    try:
        with _with_token_override(token):
            yield
    finally:
        if prev_server is None:
            os.environ.pop("AGNES_SERVER", None)
        else:
            os.environ["AGNES_SERVER"] = prev_server
        if prev_token is None:
            os.environ.pop("AGNES_TOKEN", None)
        else:
            os.environ["AGNES_TOKEN"] = prev_token


def run_pull(
    server_url: str,
    token: str,
    workspace: Path,
    *,
    dry_run: bool = False,
    skip_materialize: bool = False,
    show_progress: bool = False,
) -> PullResult:
    """Refresh local parquets + corporate memory rules from the server.

    Mirrors the `_sync_quiet` flow in `cli/commands/sync.py`, minus all
    Typer/Rich UI. Returns a `PullResult` summary; never raises for
    network/server errors (records them under `errors` instead) so the
    caller can decide whether a partial pull is fatal.

    Args:
        skip_materialize: When True, omit `query_mode='materialized'`
            tables from the download set. Use for analysts who only
            care about `--remote` access on the workspace and don't
            want to wait on multi-GB scheduled-query parquets at first
            init. Pavel's #185 Phase 1: a 6.3 GB `order_economics`
            parquet kept first init silent for 44 minutes.
        show_progress: When True, render a per-file progress bar to
            stderr via Rich during the parallel download phase. Pass
            False from `--quiet` callers (SessionStart hooks).
    """
    started = time.monotonic()
    result = PullResult()
    workspace = Path(workspace)

    with _override_server_env(server_url, token):
        # 1. Fetch manifest. A failure here means we can't tell what to
        # download at all — record the error and bail out empty-handed.
        try:
            resp = api_get("/api/sync/manifest")
            resp.raise_for_status()
            manifest = resp.json()
        except Exception as exc:
            result.errors.append({"stage": "manifest", "error": str(exc)})
            result.duration_s = time.monotonic() - started
            return result

        server_tables = manifest.get("tables", {}) or {}
        local_state = get_sync_state()
        local_tables = local_state.get("tables", {})

        # #506 — make the legacy flat `server/parquet/` tree obey the stack.
        #
        # `agnes query` reads <workspace>/user/duckdb/analytics.duckdb whose
        # views are rebuilt over <workspace>/server/parquet/*.parquet. The
        # legacy flat `manifest["tables"]` dict is gated server-side by
        # `can_access_table`, whose Admin short-circuit bypasses the stack —
        # so for an admin it over-lists every accessible table regardless of
        # subscription, and for everyone there is no prune on authorization
        # loss. The typed v49 sections (``data_packages[].tables[]`` +
        # ``direct_tables[]``) ARE stack-scoped via StackResolver, but
        # historically run_pull consumed only the flat dict. Net: removing a
        # data package dropped it from ``data_packages[]`` yet left its
        # parquet + DuckDB view locally queryable.
        #
        # When the manifest carries the query-table typed sections, the authorized
        # table-name set is the union of every typed entry's ``name`` field —
        # which equals the flat parquet stem == sync_state.table_id ==
        # registry name == _meta.table_name. We use that set both to (1) filter
        # the download set (kills admin over-listing without touching server
        # authz) and (2) prune already-downloaded parquets that left the stack.
        #
        # A pre-v49 server emits none of these keys → fall back to the flat
        # dict exactly as before (no filter, no prune). A typed-sections-present
        # but empty stack is a legitimate "subscribed to zero packages" state:
        # the authorized set is empty and ALL flat parquets are pruned, which is
        # the intended behavior (the server wraps each section builder in
        # try/except returning [] on error, and StackResolver returns [] only
        # for a genuinely empty stack — so an empty typed set is never an error
        # signal that would wrongly nuke the local tree).
        # Gate on the query-table typed sections only (``data_packages`` /
        # ``direct_tables``) — NOT ``memory_domains``. Memory domains carry no
        # query tables (no flat parquet), so a manifest that arrives with only
        # ``memory_domains`` (a partial or hand-crafted delivery) must NOT build
        # an empty authorized set and prune every local parquet. The end-of-run
        # stack-sync gate keeps ``memory_domains`` (see below) — that path
        # legitimately fires on memory domains alone.
        has_query_table_sections = any(
            k in manifest for k in ("direct_tables", "data_packages")
        )
        authorized_names: set[str] | None = None
        if has_query_table_sections:
            authorized_names = set()
            for pkg in manifest.get("data_packages", []) or []:
                for t in pkg.get("tables", []) or []:
                    name = t.get("name")
                    if name:
                        authorized_names.add(name)
            for t in manifest.get("direct_tables", []) or []:
                name = t.get("name")
                if name:
                    authorized_names.add(name)

        # 2. Compute the download set, skipping remote-mode tables (no
        # parquet on the server) and unchanged hashes.
        #
        # The parquet-existence check is load-bearing: a stale `sync_state.json`
        # entry (hash matches server) is NOT proof the file is on disk. The
        # file can disappear between runs — manual rm, disk corruption, an
        # operator nuking `server/parquet/` during cleanup, a different
        # workspace sharing the same `~/.config/agnes/sync_state.json`
        # (TODO(workspace-scoped-sync-state) below) writing one workspace's
        # parquets while another reads sync_state and assumes "I already
        # have these." Without the existence guard, `agnes pull` would skip
        # the download and the downstream DuckDB view rebuild fails on a
        # missing file. Hash-equal-but-file-missing → force re-download.
        to_download: list[str] = []
        non_remote_total = 0
        parquet_dir = workspace / "server" / "parquet"
        for tid, info in server_tables.items():
            if info.get("query_mode") == "remote":
                continue
            if skip_materialize and info.get("query_mode") == "materialized":
                # Operator opt-out for first-init. Materialized rows are
                # still discoverable via `agnes catalog` and queryable
                # the next time `agnes pull` runs without --skip-materialize.
                continue
            # #506 — when typed sections are present, the stack is the unit of
            # access: never download a flat-dict table the typed stack omits
            # (admin god-mode over-list). Pre-v49 servers have
            # `authorized_names is None` → no filter.
            if authorized_names is not None and tid not in authorized_names:
                continue
            non_remote_total += 1
            local_hash = local_tables.get(tid, {}).get("hash", "")
            server_hash = info.get("hash", "")
            target = parquet_dir / f"{tid}.parquet"
            if (
                server_hash != local_hash
                or tid not in local_tables
                or not server_hash
                or not target.exists()
            ):
                to_download.append(tid)
        result.parquets_total = non_remote_total

        # 3. Dry-run short-circuit — touch nothing on disk.
        if dry_run:
            result.tables_updated = 0  # by definition no writes happened
            result.duration_s = time.monotonic() - started
            return result

        # 4. Download parquets in parallel. Lazy mkdir: only create
        # server/parquet/ when we have at least one table to write into it.
        # Concurrency capped by `AGNES_PULL_PARALLELISM` (default 4) so a
        # registry of 50+ tables doesn't open 50+ TCP connections + saturate
        # the analyst's NIC; 4 matches typical home-broadband saturation
        # without over-subscribing the server's caddy file_server (each
        # request is a separate goroutine + sendfile, but the analyst's
        # downlink is the more frequent bottleneck). Set to 1 to restore
        # the pre-PR serial behavior for debug repro. The server-side
        # bypass-uvicorn fix (Caddy file_server) is the other half —
        # without it, parallel downloads would still queue on the single
        # uvicorn worker.
        if to_download and not parquet_dir.exists():
            parquet_dir.mkdir(parents=True, exist_ok=True)

        try:
            workers = max(1, int(os.environ.get("AGNES_PULL_PARALLELISM", "4")))
        except ValueError:
            workers = 4
        # Drop to serial when there's only one (or zero) tables — avoids
        # the executor + thread overhead for the common single-update case.
        workers = min(workers, len(to_download)) if to_download else 1

        # Optional progress reporting — two paths.
        #
        # 1. Rich progress bar: per-file bytes-streamed bar with speed +
        #    ETA. Rendered to stderr when stderr is a TTY. Aggregates
        #    across the parallel ThreadPoolExecutor workers and across
        #    chunked-download chunks (all chunks call the same callback
        #    advancing the same task).
        # 2. Textual fallback: when `show_progress=True` but stderr is
        #    NOT a TTY (Claude Code SessionStart hook, CI run, Docker
        #    log capture), Rich would either suppress the bar or emit
        #    raw control sequences. Instead we emit one plain-text line
        #    per file at most every 10% or 30 s — enough signal to know
        #    the pull isn't frozen on a multi-GB parquet, terse enough
        #    not to spam the consumer's log.
        #
        # Both paths receive the same per-file callback so the chunked-
        # download contract ("one file = one task, sum-of-chunks bytes")
        # is honored uniformly.
        import sys as _sys
        progress = None
        progress_tasks: dict[str, int] = {}
        textual = None
        use_textual_fallback = (
            show_progress
            and to_download
            and not _sys.stderr.isatty()
        )
        if show_progress and to_download and not use_textual_fallback:
            from rich.progress import (
                Progress, BarColumn, DownloadColumn, TextColumn,
                TimeRemainingColumn, TransferSpeedColumn,
            )
            progress = Progress(
                TextColumn("[bold]{task.fields[label]}[/]"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                transient=False,
            )
            progress.start()
            for tid in to_download:
                size = int(server_tables[tid].get("size_bytes") or 0)
                # Some manifest entries don't carry size — Rich shows
                # an indeterminate bar in that case.
                progress_tasks[tid] = progress.add_task(
                    "download", label=tid, total=size if size > 0 else None,
                )
        elif use_textual_fallback:
            textual = _TextualProgress(
                stream=_sys.stderr,
                total_files=len(to_download),
                file_sizes={
                    tid: int(server_tables[tid].get("size_bytes") or 0)
                    for tid in to_download
                },
            )

        def _download_one(tid: str) -> tuple[str, dict | None, str | None]:
            """Returns (tid, local_table_entry_or_None, error_or_None).
            One bound thread per call; stream_download is sync I/O so a
            ThreadPoolExecutor (not asyncio) is the right tool. The
            progress callback is thread-safe — Rich's Progress.update
            and the textual fallback's lock both serialize internally.

            Durability contract (#596): the prior good `<tid>.parquet`
            (if any) is NEVER unlinked before a fresh download has
            verified. The download lands in a sidecar
            `<tid>.parquet.verify.tmp`, the hash (or, on a hash-less
            legacy manifest, the PAR1 structural check) is checked
            there, and only on success is the sidecar `os.replace`d into
            the final target — atomic, so a reader never sees a
            half-written or mismatched file. A hash mismatch is treated
            as transient: the download+verify is retried up to
            ``_DOWNLOAD_RETRIES`` times (small backoff between attempts)
            before giving up. On persistent failure the sidecar is
            removed, the OLD good parquet stays in place, and the table
            is recorded under ``result.errors`` — the table is never
            left missing from disk."""
            target = parquet_dir / f"{tid}.parquet"
            sidecar = parquet_dir / f"{tid}.parquet.verify.tmp"
            expected_hash = server_tables[tid].get("hash", "")
            cb = None
            if progress is not None and tid in progress_tasks:
                task_id = progress_tasks[tid]
                def cb(n: int, _tid=tid, _task=task_id):
                    progress.update(_task, advance=n)
            elif textual is not None:
                def cb(n: int, _tid=tid):
                    textual.advance(_tid, n)

            last_err: str | None = None
            try:
                for attempt in range(_DOWNLOAD_RETRIES + 1):
                    try:
                        # Download into a sidecar — the real target keeps
                        # the prior good bytes until verification passes.
                        stream_download(
                            f"/api/data/{tid}/download", str(sidecar),
                            progress_callback=cb,
                        )
                        if expected_hash:
                            actual_hash = _file_md5(sidecar)
                            if actual_hash != expected_hash:
                                last_err = (
                                    f"hash mismatch: expected "
                                    f"{expected_hash[:12]}, got {actual_hash[:12]}"
                                )
                                sidecar.unlink(missing_ok=True)
                                # Re-download on mismatch before giving up.
                                if attempt < _DOWNLOAD_RETRIES:
                                    time.sleep(
                                        _DOWNLOAD_RETRY_BACKOFFS_S[
                                            min(attempt, len(_DOWNLOAD_RETRY_BACKOFFS_S) - 1)
                                        ]
                                    )
                                    continue
                                # Persistent mismatch: prior good target
                                # (if any) is untouched; record + bail.
                                return tid, None, last_err
                        elif not _is_valid_parquet(sidecar):
                            # Pre-v49 / no-hash legacy path — unchanged
                            # semantics, just verified on the sidecar.
                            sidecar.unlink(missing_ok=True)
                            raise ValueError(
                                "not a valid parquet (missing PAR1 magic)"
                            )
                        # Verified — promote the sidecar atomically.
                        os.replace(sidecar, target)
                        entry = {
                            "hash": expected_hash,
                            "rows": server_tables[tid].get("rows", 0),
                            "size_bytes": server_tables[tid].get("size_bytes", 0),
                        }
                        return tid, entry, None
                    except Exception as exc:
                        last_err = str(exc)
                        sidecar.unlink(missing_ok=True)
                        if attempt < _DOWNLOAD_RETRIES:
                            time.sleep(
                                _DOWNLOAD_RETRY_BACKOFFS_S[
                                    min(attempt, len(_DOWNLOAD_RETRY_BACKOFFS_S) - 1)
                                ]
                            )
                            continue
                        return tid, None, last_err
                # Loop exhausted without an explicit return (defensive).
                return tid, None, last_err or "download failed"
            finally:
                sidecar.unlink(missing_ok=True)

        try:
            if workers <= 1:
                outcomes = [_download_one(tid) for tid in to_download]
            else:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    outcomes = list(ex.map(_download_one, to_download))
        finally:
            if progress is not None:
                progress.stop()
            if textual is not None:
                textual.finish()

        for tid, entry, err in outcomes:
            if err is not None:
                result.errors.append({"table": tid, "error": err})
            else:
                local_tables[tid] = entry
                result.tables_updated += 1

        # 4b. #506 — prune local parquets that left the authorized typed
        # stack. Runs only when the manifest carries typed sections (else
        # ``authorized_names is None`` and this is a no-op — pre-v49 servers
        # are untouched). For any ``server/parquet/<stem>.parquet`` on disk
        # whose stem is not authorized, unlink the file and drop its
        # ``local_tables[stem]`` sync_state row. The unconditional view
        # rebuild in step 6 then drops the now-orphaned view automatically
        # (it DROPs all views, then recreates only from parquets still on
        # disk). Remote tables have no flat parquet so they're untouched;
        # materialized tables DO have a flat parquet and are pruned like any
        # other table when they leave the stack (intended). User-created BASE
        # TABLEs live in analytics.duckdb (not under server/parquet/) so they're
        # never pruned. Done before
        # save_sync_state so the dropped rows persist, and before
        # _rebuild_duckdb_views so the orphaned views disappear.
        if authorized_names is not None and parquet_dir.exists():
            for pq_file in sorted(parquet_dir.glob("*.parquet")):
                stem = pq_file.stem
                if stem in authorized_names:
                    continue
                pq_file.unlink(missing_ok=True)
                local_tables.pop(stem, None)
                result.tables_removed += 1

        # 5. Persist sync state (only on real runs).
        # TODO(workspace-scoped-sync-state): currently saved to
        # ~/.config/agnes/sync_state.json (per legacy sync.py behavior).
        # Two workspaces sharing one user account share this state.
        # Future: scope to <workspace>/.agnes/sync_state.json so workspace
        # bootstrap leaves no residue outside <workspace>/.
        local_state["tables"] = local_tables
        local_state["last_sync"] = datetime.now(timezone.utc).isoformat()
        save_sync_state(local_state)

        # 6. Rebuild DuckDB views — unconditional. The DB file is the
        # load-bearing artifact for downstream readers.
        _rebuild_duckdb_views(workspace, parquet_dir)

        # 7. Fetch corporate-memory bundle and lazily write
        # `.claude/rules/km_*.md`. Best-effort: a server outage on this
        # endpoint must not fail the whole pull.
        try:
            written = _fetch_and_write_rules(workspace)
            result.rules_count = written
        except Exception as exc:
            result.errors.append({"stage": "memory_bundle", "error": str(exc)})

        # 8. v49 stack sync — per-type loop into ``~/.claude/data/`` and
        # ``~/.claude/memory/`` with reference-counted dedup. Runs only
        # when the manifest carries the v49 fields (older servers /
        # backward-compat workspaces are untouched). Best-effort:
        # failure here records under ``result.errors`` but doesn't abort
        # the rest of the pull.
        if any(
            k in manifest for k in ("direct_tables", "data_packages", "memory_domains")
        ):
            try:
                result.stack_sync = _run_stack_sync_from_manifest(manifest, workspace)
            except Exception as exc:
                result.errors.append({"stage": "stack_sync", "error": str(exc)})

    result.duration_s = time.monotonic() - started

    # 9. Pull-confirm telemetry — fire-and-forget POST so the server can
    # close the loop on the ``sync.pull_started`` event from Phase 6.
    try:
        _emit_pull_confirm(server_url, token, result)
    except Exception:
        pass

    return result


def _run_stack_sync_from_manifest(manifest: dict, workspace: Path):
    """Build a ``pull_sync.PullStackOptions`` from the manifest payload
    and invoke ``run_stack_sync``. The local sync root is the
    ``<workspace>/.claude/`` dir so the stack-sync artifacts live next
    to the existing ``<workspace>/.claude/rules/`` / ``<workspace>/.claude/
    settings.json`` tree (workspace-scoped, not user-home, matching
    Section 5.3 of the spec for analyst workspaces)."""
    from cli.lib.pull_sync import PullStackOptions, run_stack_sync

    local_root = workspace / ".claude"

    def _fetcher(url: str, target: Path) -> None:
        stream_download(url, str(target))

    def _bundle_fetcher(slug: str) -> bytes:
        resp = api_get("/api/memory/bundle", params={"domain": slug})
        resp.raise_for_status()
        return resp.content

    opts = PullStackOptions(
        manifest=manifest,
        local_dir=local_root,
        fetcher=_fetcher,
        md5_of=_file_md5,
        bundle_fetcher=_bundle_fetcher,
    )
    return run_stack_sync(opts)


def _emit_pull_confirm(server_url: str, token: str, result: "PullResult") -> None:
    """POST /api/sync/pull-confirm with the per-type aggregate counts.

    Fire-and-forget — the parent already swallows exceptions but the
    helper has its own ``try/except`` so a 404 (older server without
    the endpoint) is silent rather than logged as a warning."""
    stack = result.stack_sync
    direct = getattr(stack, "direct_tables", None) if stack else None
    dp = getattr(stack, "data_packages", None) if stack else None
    md = getattr(stack, "memory_domains", None) if stack else None
    payload = {
        "duration_ms": int(result.duration_s * 1000),
        "direct_tables": {
            "added": getattr(direct, "added", 0),
            "updated": getattr(direct, "updated", 0),
            "removed": getattr(direct, "removed", 0),
        },
        "data_packages": {
            "added": getattr(dp, "added", 0),
            "updated": getattr(dp, "updated", 0),
            "removed": getattr(dp, "removed", 0),
        },
        "memory_domains": {
            "added": getattr(md, "added", 0),
            "updated": getattr(md, "updated", 0),
            "removed": getattr(md, "removed", 0),
        },
        "errors": len(result.errors),
    }
    try:
        api_post("/api/sync/pull-confirm", json=payload)
    except Exception:
        # Endpoint may not exist on older servers; silent skip.
        pass


# ---------------------------------------------------------------------------
# Helpers — copied verbatim from cli/commands/sync.py with the lazy-mkdir
# fix in `_fetch_and_write_rules`. Task 18 deletes sync.py; until then the
# two copies coexist (no behavior drift, copy not move).
# ---------------------------------------------------------------------------


def _file_md5(path: Path) -> str:
    """MD5 of a file, same chunking as app/api/sync.py:_file_hash so the
    client-side verification matches the manifest hash byte-for-byte."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_valid_parquet(path: Path) -> bool:
    """Cheap structural check — parquet files begin and end with `PAR1`.

    Used as a fallback when the manifest has no hash (legacy snapshots) and
    during view rebuild to skip obviously-broken files. Does not guarantee
    the footer is well-formed — that's DuckDB's job at CREATE VIEW time.
    """
    try:
        size = path.stat().st_size
        if size < 8:
            return False
        with open(path, "rb") as f:
            head = f.read(4)
            f.seek(-4, 2)
            tail = f.read(4)
        return head == b"PAR1" and tail == b"PAR1"
    except OSError:
        return False


def _rebuild_duckdb_views(workspace: Path, parquet_dir: Path) -> None:
    """Recreate DuckDB views from downloaded parquets. Preserve user tables.

    The DuckDB file at `<workspace>/user/duckdb/analytics.duckdb` is
    created unconditionally (even on an empty pull) — downstream readers
    expect the file to exist. The parquet rebuild loop is a no-op when
    `parquet_dir` is missing.
    """
    import duckdb  # noqa: F401  (kept for the duckdb.Error path below)
    from src.duckdb_conn import _open_duckdb

    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open_duckdb(str(db_path))
    try:
        # Existing user-created BASE TABLEs we must not shadow with views.
        try:
            existing_tables = {
                row[0] for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_type='BASE TABLE'"
                ).fetchall()
            }
        except Exception:
            existing_tables = set()

        # Drop all current views so the rebuild is from a clean slate.
        try:
            views = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
            ).fetchall()
            for (view_name,) in views:
                conn.execute(f'DROP VIEW IF EXISTS "{view_name}"')
        except Exception:
            pass

        # Recreate views for each parquet file. One broken file (corrupt
        # download, partial write left over from a previous run, ...) must
        # not abort the whole rebuild — skip and keep going.
        if parquet_dir.exists():
            for pq_file in parquet_dir.rglob("*.parquet"):
                view_name = pq_file.stem
                if view_name in existing_tables:
                    continue
                if not _is_valid_parquet(pq_file):
                    continue
                abs_path = str(pq_file.resolve())
                try:
                    conn.execute(
                        f'CREATE VIEW "{view_name}" AS '
                        f"SELECT * FROM read_parquet('{abs_path}')"
                    )
                except duckdb.Error:
                    continue
    finally:
        conn.close()


def _item_to_md(item: dict) -> str:
    """Render a knowledge item as a Markdown rule file."""
    lines = [f"# {item.get('title', 'Untitled')}"]
    if item.get("domain"):
        lines.append(f"_Domain: {item['domain']}_")
    if item.get("category"):
        lines.append(f"_Category: {item['category']}_")
    lines.append("")
    lines.append(item.get("content", ""))
    return "\n".join(lines)


def _fetch_and_write_rules(workspace: Path) -> int:
    """Fetch /api/memory/bundle and write `.claude/rules/km_*.md` files.

    Returns the count of rule files actually written.

    Lazy mkdir contract — Task 8 fix vs. legacy `cli/commands/sync.py`:
    the rules directory is created only when the bundle has at least one
    mandatory item or a non-empty approved list. An empty bundle leaves
    the workspace untouched (no `.claude/rules/` shell, no `km_approved.md`
    cleanup attempt against a directory that doesn't exist).

    The km_*.md namespace in `.claude/rules/` is server-managed: this
    function is the only writer, and it prunes any stale km_*.md files on
    every run that materializes the directory. Do not create km_*.md
    files manually — they will be removed on next pull.
    """
    rules_dir = workspace / ".claude" / "rules"
    resp = api_get("/api/memory/bundle")
    resp.raise_for_status()
    bundle = resp.json()

    mandatory = bundle.get("mandatory", []) or []
    approved = bundle.get("approved", []) or []

    # Lazy mkdir — empty bundle leaves the workspace tree alone.
    if not mandatory and not approved:
        return 0

    rules_dir.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()

    # One file per mandatory item.
    for item in mandatory:
        item_id = item.get("id", "")
        if not _SAFE_ID_RE.match(item_id):
            # Silently skip unsafe ids — caller has no Typer.echo here.
            continue
        fname = f"km_{item_id}.md"
        (rules_dir / fname).write_text(_item_to_md(item), encoding="utf-8")
        written.add(fname)

    # Approved items roll up into a single file.
    if approved:
        lines = ["# Approved Corporate Knowledge\n"]
        for item in approved:
            lines.append(f"## {item.get('title', 'Untitled')}\n")
            lines.append(item.get("content", "") + "\n")
        (rules_dir / "km_approved.md").write_text("\n".join(lines), encoding="utf-8")
        written.add("km_approved.md")
    else:
        stale = rules_dir / "km_approved.md"
        if stale.exists():
            stale.unlink()

    # Prune stale per-item files no longer mandatory.
    for existing in rules_dir.glob("km_*.md"):
        if existing.name not in written and existing.name != "km_approved.md":
            existing.unlink()

    return len(written)
