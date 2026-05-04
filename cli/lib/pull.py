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

from cli.client import api_get, stream_download
from cli.config import get_sync_state, save_sync_state


@dataclass
class PullResult:
    """Outcome of a `run_pull` invocation.

    Fields:
    - `tables_updated`: count of parquets actually re-downloaded this run.
    - `parquets_total`: count of non-remote tables visible in the manifest.
    - `rules_count`: number of `km_*.md` files written to `.claude/rules/`.
    - `duration_s`: wall time of the call.
    - `errors`: list of `{"table": ..., "error": ...}` (or
      `{"stage": "memory_bundle", "error": ...}`) — best-effort flow,
      individual failures don't abort the whole pull.
    """

    tables_updated: int = 0
    parquets_total: int = 0
    rules_count: int = 0
    duration_s: float = 0.0
    errors: list[dict] = field(default_factory=list)


_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")


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
) -> PullResult:
    """Refresh local parquets + corporate memory rules from the server.

    Mirrors the `_sync_quiet` flow in `cli/commands/sync.py`, minus all
    Typer/Rich UI. Returns a `PullResult` summary; never raises for
    network/server errors (records them under `errors` instead) so the
    caller can decide whether a partial pull is fatal.
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

        # 4. Download parquets. Lazy mkdir: only create server/parquet/
        # when we have at least one table to write into it.
        for tid in to_download:
            if not parquet_dir.exists():
                parquet_dir.mkdir(parents=True, exist_ok=True)
            target = parquet_dir / f"{tid}.parquet"
            expected_hash = server_tables[tid].get("hash", "")
            try:
                stream_download(f"/api/data/{tid}/download", str(target))
                if expected_hash:
                    actual_hash = _file_md5(target)
                    if actual_hash != expected_hash:
                        target.unlink(missing_ok=True)
                        raise ValueError(
                            f"hash mismatch: expected {expected_hash[:12]}, got {actual_hash[:12]}"
                        )
                elif not _is_valid_parquet(target):
                    target.unlink(missing_ok=True)
                    raise ValueError("not a valid parquet (missing PAR1 magic)")
                local_tables[tid] = {
                    "hash": expected_hash,
                    "rows": server_tables[tid].get("rows", 0),
                    "size_bytes": server_tables[tid].get("size_bytes", 0),
                }
                result.tables_updated += 1
            except Exception as exc:
                result.errors.append({"table": tid, "error": str(exc)})

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

    result.duration_s = time.monotonic() - started
    return result


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
    import duckdb

    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
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
