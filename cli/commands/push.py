"""`agnes push` — upload session jsonls + CLAUDE.local.md to the server.

The push command consumes a workspace-local queue file
(``<workspace>/.claude/agnes-sessions.txt``) populated by the
``agnes capture-session`` SessionStart hook. Each line is the absolute
path to a session jsonl. This avoids reverse-engineering Claude Code's
internal cwd-to-folder encoding (which varies by version).

Concurrency: a single-instance lock (``filelock`` via ``cli/lib/push_lock.py``)
ensures only one push runs at a time. When the user closes several Claude
Code sessions simultaneously, every SessionEnd hook fires its own
``agnes push``; exactly one runs, the rest exit silently.

Race protection: the queue file is atomically renamed to a snapshot before
processing. SessionStart hooks that fire during the push window write to
a freshly-created queue, so their entries aren't lost.

Recovery: if a previous push crashed mid-run, its snapshot file persists.
The next push picks it up before processing the current queue.

Legacy fallback: the encoding-based ``list_session_files`` path remains
available behind ``--legacy-scan`` for one-off backfills of sessions that
predate the queue mechanism.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import typer

from cli.client import api_post
from cli.config import get_server_url, get_token
from cli.error_render import render_error
from cli.lib.push_lock import acquire_or_skip
from cli.lib.session_queue import (
    discard_snapshot,
    find_recovery_snapshots,
    mark_uploaded,
    queue_path,
    read_paths_from_snapshot,
    requeue_failed,
    snapshot_queue,
    uploaded_log_path,
)


push_app = typer.Typer(help="Upload sessions and CLAUDE.local.md to the server")


def _collect_snapshots(workspace: Path) -> list[Path]:
    """Recovery snapshots first (oldest first), then a fresh snapshot of the
    current queue. Either may be absent. Returns the list of snapshot paths
    to process in order.
    """
    snapshots = find_recovery_snapshots(workspace)
    fresh = snapshot_queue(workspace)
    if fresh is not None:
        snapshots.append(fresh)
    return snapshots


def _gather_paths_for_dry_run(workspace: Path) -> list[Path]:
    """Read paths that *would* be uploaded without consuming the queue.

    Combines existing recovery snapshots + the current live queue. Does NOT
    rename the live queue (dry-run is read-only).
    """
    paths: list[Path] = []
    seen: set[str] = set()

    for snap in find_recovery_snapshots(workspace):
        for p in read_paths_from_snapshot(snap):
            s = str(p)
            if s in seen:
                continue
            seen.add(s)
            paths.append(p)

    live = queue_path(workspace)
    if live.exists():
        for p in read_paths_from_snapshot(live):
            s = str(p)
            if s in seen:
                continue
            seen.add(s)
            paths.append(p)

    return paths


def _upload_one(transcript: Path) -> tuple[bool, dict]:
    """Upload a single session jsonl. Returns (success, error_or_meta)."""
    if not transcript.exists():
        return False, {"file": transcript.name, "error": "file not found on disk"}
    try:
        with open(transcript, "rb") as fh:
            resp = api_post("/api/upload/sessions", files={"file": (transcript.name, fh)})
    except Exception as exc:
        return False, {"file": transcript.name, "error": str(exc)}
    if resp.status_code == 200:
        return True, {"file": transcript.name}
    return False, {"file": transcript.name, "status": resp.status_code}


@push_app.callback(invoke_without_command=True)
def push(
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Suppress success stdout (errors still surface on stderr).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit a single JSON object summarizing the upload."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List what would be uploaded without sending anything.",
    ),
    legacy_scan: bool = typer.Option(
        False,
        "--legacy-scan",
        help=(
            "Fallback: also include sessions found by the encoding-based scan "
            "of ~/.claude/projects/. Use for one-off backfill of sessions "
            "predating the queue mechanism."
        ),
    ),
):
    """Upload queued session jsonls + CLAUDE.local.md from this workspace."""
    server_url = get_server_url()
    if not server_url:
        typer.echo(
            render_error(0, {"detail": {
                "kind": "server_unreachable",
                "hint": "No server configured. Run: agnes init --server-url <URL> --token <PAT>",
            }}),
            err=True,
        )
        raise typer.Exit(1)

    token = get_token()
    if not token:
        typer.echo(
            render_error(0, {"detail": {
                "kind": "auth_failed",
                "hint": "No token. Run: agnes auth import-token --token <PAT>",
            }}),
            err=True,
        )
        raise typer.Exit(1)

    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()
    local_md = workspace / ".claude" / "CLAUDE.local.md"
    has_local_md = local_md.exists()

    # ---- DRY RUN ----------------------------------------------------------
    if dry_run:
        candidates = _gather_paths_for_dry_run(workspace)
        if legacy_scan:
            from cli.lib.claude_sessions import list_session_files
            seen = {str(p) for p in candidates}
            for p in list_session_files(workspace):
                if str(p) not in seen:
                    candidates.append(p)
                    seen.add(str(p))

        plan = {
            "dry_run": True,
            "would_upload": {
                "sessions": [str(p) for p in candidates],
                "local_md": str(local_md) if has_local_md else None,
            },
            "summary": {
                "sessions_count": len(candidates),
                "local_md_present": has_local_md,
                "uploaded_log": str(uploaded_log_path(workspace)),
            },
        }
        if as_json:
            typer.echo(json.dumps(plan, indent=2))
            return
        if quiet:
            return
        typer.echo(f"Dry run - would upload {len(candidates)} session file(s)")
        for p in candidates:
            typer.echo(f"  {p}")
        if has_local_md:
            typer.echo(f"Would upload CLAUDE.local.md  ({local_md})")
        else:
            typer.echo("No CLAUDE.local.md to upload")
        return

    # ---- REAL RUN ---------------------------------------------------------
    # Acquire single-instance lock. Silent exit if another push is already
    # running — typical when several SessionEnd hooks fire at once.
    with acquire_or_skip(workspace) as lock:
        if lock is None:
            return  # another push has the lock; this one no-ops

        results = {"sessions": 0, "local_md": False, "errors": [], "skipped": 0}

        # Process snapshots: recovery (from prior crash) first, then fresh.
        snapshots = _collect_snapshots(workspace)
        all_failed_paths: list[Path] = []

        for snapshot in snapshots:
            paths = read_paths_from_snapshot(snapshot)
            failed_in_snapshot: list[Path] = []
            for transcript in paths:
                ok, info = _upload_one(transcript)
                if ok:
                    results["sessions"] += 1
                    mark_uploaded(workspace, transcript, datetime.now(timezone.utc))
                else:
                    if info.get("error") == "file not found on disk":
                        # Stale queue entry (Claude Code auto-cleanup deleted
                        # the jsonl). Skip without re-queuing — retry would
                        # loop forever.
                        results["skipped"] += 1
                        results["errors"].append(info)
                    else:
                        results["errors"].append(info)
                        failed_in_snapshot.append(transcript)
            # Failed paths from this snapshot get re-queued on the live file.
            all_failed_paths.extend(failed_in_snapshot)
            discard_snapshot(snapshot)

        if all_failed_paths:
            requeue_failed(workspace, all_failed_paths)

        # Optional: legacy scan to backfill sessions outside the queue.
        if legacy_scan:
            from cli.lib.claude_sessions import list_session_files
            for transcript in list_session_files(workspace):
                ok, info = _upload_one(transcript)
                if ok:
                    results["sessions"] += 1
                    mark_uploaded(workspace, transcript, datetime.now(timezone.utc))
                else:
                    results["errors"].append(info)

        # Upload CLAUDE.local.md.
        if has_local_md:
            try:
                content = local_md.read_text(encoding="utf-8")
                resp = api_post("/api/upload/local-md", json={"content": content})
                if resp.status_code == 200:
                    results["local_md"] = True
                else:
                    results["errors"].append(
                        {"file": "CLAUDE.local.md", "status": resp.status_code}
                    )
            except Exception as exc:
                results["errors"].append({"file": "CLAUDE.local.md", "error": str(exc)})

    # Render output.
    if as_json:
        typer.echo(json.dumps(results))
        return

    if quiet:
        if results["errors"]:
            for e in results["errors"]:
                typer.echo(f"warn: {e}", err=True)
        return

    typer.echo(f"Uploaded {results['sessions']} sessions")
    if results["skipped"]:
        typer.echo(f"Skipped {results['skipped']} stale queue entries (file missing)")
    if results["local_md"]:
        typer.echo("Uploaded CLAUDE.local.md")
    if results["errors"]:
        for e in results["errors"]:
            typer.echo(f"warn: {e}", err=True)
