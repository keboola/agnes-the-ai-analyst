"""`agnes status` — workspace status: initialized? data fresh? hooks active?

Server-health checks live under `agnes diagnose system` (see the
`agnes diagnose` group).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer

# Mirrors the dual-marker convention documented in cli/commands/init.py:
# `.claude/init-complete` is the authoritative sentinel written by every
# successful init (default OR Initial-Workspace-override mode); the legacy
# CLAUDE.md substring is kept as a fallback for pre-#259 workspaces. The
# sentinel-first ordering matters for override workspaces AND all
# post-rebrand default workspaces: neither contains the literal "AI Data
# Analyst" substring (the marker is hardcoded against the pre-rebrand
# default template's `# {{ instance.name }} — AI Data Analyst` heading),
# and the legacy grep alone would then falsely report "Initialized: no"
# even when init wrote the sentinel and the workspace is functional.
_INIT_SENTINEL = Path(".claude") / "init-complete"
_INIT_MARKER = "AI Data Analyst"


def _count_local_tables(workspace: Path) -> int:
    """Distinct tables queryable from this workspace's parquet trees.

    Counts TABLES, not files — the two are only the same for a single-file
    table. Both trees `agnes pull` writes are counted, de-duplicated by
    table id because a table can legitimately appear in both:

    - ``server/parquet/`` (the legacy flat flow) holds ``<tid>.parquet``
      for a single-file table and ``<tid>/**/*.parquet`` for a partitioned
      one. The previous non-recursive ``glob("*.parquet")`` returned 0 for
      a partitioned table no matter how many parts were on disk, so a
      workspace of Jira tables reported ``Parquets: 0`` while holding
      hundreds of part files.
    - ``.claude/data/_shared/<tid>.parquet`` is the v49 stack sync's
      canonical store, which the old count ignored entirely. Its
      ``_direct/`` and ``<package_slug>/`` siblings are reference links
      back into ``_shared`` (see ``cli/lib/pull_sync.py``), so counting
      ``_shared`` alone is what avoids counting one table once per package
      that ships it.

    A directory with no parquet under it does not count: an interrupted
    partitioned sync leaves an empty ``<tid>/`` behind, and there is
    nothing queryable in it.
    """
    table_ids: set[str] = set()

    legacy_dir = workspace / "server" / "parquet"
    if legacy_dir.is_dir():
        for entry in sorted(legacy_dir.iterdir()):
            # Interrupted partitioned syncs leave `.staging-<tid>` scratch
            # dirs; the view rebuild skips them, so must the count.
            if entry.name.startswith(".staging-"):
                continue
            if entry.is_dir():
                if any(entry.rglob("*.parquet")):
                    table_ids.add(entry.name)
            elif entry.suffix == ".parquet":
                table_ids.add(entry.stem)

    shared_dir = workspace / ".claude" / "data" / "_shared"
    if shared_dir.is_dir():
        for parquet in shared_dir.glob("*.parquet"):
            table_ids.add(parquet.stem)

    return len(table_ids)


status_app = typer.Typer(help="Show workspace status (initialized? data fresh? hooks active?)")


@status_app.callback(invoke_without_command=True)
def status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    from cli.lib.workspace_resolve import resolve_data_workspace

    workspace = resolve_data_workspace() or Path.cwd().resolve()

    initialized = (workspace / _INIT_SENTINEL).exists()
    if not initialized:
        claude_md = workspace / "CLAUDE.md"
        if claude_md.exists():
            try:
                initialized = _INIT_MARKER in claude_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                initialized = False

    table_count = _count_local_tables(workspace)

    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    last_synced = None
    if db_path.exists():
        last_synced = datetime.fromtimestamp(db_path.stat().st_mtime, tz=UTC).isoformat()

    # Sessions live in <projects_root>/<encoded-workspace_root>/ where Claude
    # Code writes them. Count what `agnes push` would scan — anchored on the
    # `workspace_root` config key (the same anchor push uses), so a status run
    # from any cwd reports the real workspace. 0 when unset.
    from cli.config import get_workspace_root
    from cli.lib.session_paths import list_session_files

    ws_root = get_workspace_root()
    session_count = len(list_session_files(Path(ws_root))) if ws_root else 0

    info = {
        "workspace": str(workspace),
        "initialized": initialized,
        "parquet_tables": table_count,
        "duckdb_exists": db_path.exists(),
        "last_synced": last_synced,
        "sessions_pending_upload": session_count,
    }

    if as_json:
        typer.echo(json.dumps(info, indent=2))
        return

    typer.echo(f"Workspace : {workspace}")
    typer.echo(f"Initialized: {'yes' if initialized else 'no'}")
    typer.echo(f"Tables    : {info['parquet_tables']}")
    typer.echo(f"DuckDB    : {'yes' if info['duckdb_exists'] else 'no'}")
    typer.echo(f"Last sync : {last_synced or 'never'}")
    typer.echo(f"Pending uploads: {session_count} sessions")

    if not initialized:
        typer.echo("")
        if table_count:
            # A workspace can hold data while carrying no init sentinel, and
            # reporting a bare "no" next to a populated `Tables` line reads as
            # a contradiction. The two ask different questions: `agnes pull`
            # only needs the directory to be workspace-*shaped*
            # (`is_workspace_shaped` in cli/lib/workspace_resolve.py accepts a
            # bare `server/parquet/`), whereas "initialized" means `agnes init`
            # ran HERE and installed the hooks + template. Name the half
            # that is actually missing instead of implying the data is not there.
            typer.echo(
                f"This workspace holds data ({table_count} tables) but `agnes init` "
                "never ran here — no Claude Code hooks, no workspace template."
            )
            typer.echo("Run `agnes init --server-url <URL> --token <PAT>` to finish setting it up.")
        else:
            typer.echo("Run `agnes init --server-url <URL> --token <PAT>` to bootstrap.")
