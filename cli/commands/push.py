"""`agnes push` — scan the workspace's Claude Code session folder and upload
new/grown transcripts (+ CLAUDE.local.md) to the server.

Mechanism (replaces the former stdin-capture + queue, which was unreliable on
macOS where Claude Code delivers empty hook stdin): the workspace root is read
from the Agnes config (``workspace_root``, written by ``agnes init`` and
back-filled by ``agnes self-upgrade``). push encodes it to Claude Code's
projects-dir folder name (``cli/lib/session_paths.py``) and lists the
``*.jsonl`` transcripts there. Each file's stem is its ``session_id``.

Dedup is by ``session_id`` + byte size against the upload ledger
(``cli/lib/upload_log.py``): unseen -> upload; same size -> skip; larger size
(the transcript grew) -> re-upload. The server overwrites by filename, so a
re-upload is idempotent. The ledger row is appended immediately after each
success, so an interrupted push never re-uploads a completed file next run.

No ``workspace_root`` in config -> nothing to find: push exits 0 without
uploading anything (sessions OR CLAUDE.local.md — both are anchored to the
same root, so without it neither can be located). Works identically on
Windows and macOS, with no dependency on hook stdin.

Concurrency: a single-instance lock (``cli/lib/push_lock.py``) means only one
push runs when several SessionEnd hooks fire at once; the rest exit silently.

Private filter: a session whose id is on the ``/agnes-private`` list
(``cli/lib/private_list.py``) is never uploaded; the skip is audit-logged to
``agnes-sessions-private-skipped.txt``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import typer

from cli.client import api_post
from cli.config import get_server_url, get_token, get_workspace_root
from cli.error_render import render_error
from cli.lib.private_list import read_all_private
from cli.lib.push_lock import acquire_or_skip
from cli.lib.session_paths import list_session_files
from cli.lib.upload_log import (
    mark_failed_permanent,
    mark_private_skipped,
    mark_uploaded,
    read_uploaded,
    uploaded_log_path,
)


push_app = typer.Typer(help="Upload sessions and CLAUDE.local.md to the server")


def _is_permanent_failure(info: dict) -> bool:
    """True iff the server response is a deterministic failure retrying won't fix.

    4xx except 401 / 408 / 429: 403 (RBAC), 413 (too large), 400 (validation)
    all re-produce the same answer on re-upload, so we log them to the
    forensic failed-log instead of retrying forever. 401 is recoverable
    (PAT expired — re-auth makes the same upload succeed), and 408 / 429 are
    transient per HTTP spec; all three (plus 5xx and network errors) are left
    unrecorded so the next push retries.
    """
    status = info.get("status")
    if not isinstance(status, int):
        return False  # network error / exception — transient
    if status in (401, 408, 429):
        return False
    return 400 <= status < 500


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
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON object summarizing the upload."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List what would be uploaded without sending anything.",
    ),
):
    """Upload new/grown session jsonls + CLAUDE.local.md from this workspace."""
    server_url = get_server_url()
    if not server_url:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "server_unreachable",
                        "hint": "No server configured. Run: agnes init --server-url <URL> --token <PAT>",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    token = get_token()
    if not token:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "auth_failed",
                        "hint": "No token. Run: agnes auth import-token --token <PAT>",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    # The workspace root is the ONLY anchor. Without it we can't locate the
    # Claude Code session folder OR the workspace's CLAUDE.local.md, so push
    # is a clean no-op (exit 0). `agnes init` writes it; `agnes self-upgrade`
    # back-fills it on the next SessionStart for older clients.
    workspace_root = get_workspace_root()
    if not workspace_root:
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "sessions": 0,
                        "local_md": False,
                        "errors": [],
                        "private_skipped": 0,
                        "skipped_unchanged": 0,
                        "workspace_root": None,
                    }
                )
            )
        elif not quiet:
            typer.echo(
                "No workspace_root in config — nothing to upload. Run `agnes init` to set it.",
                err=True,
            )
        return

    workspace = Path(workspace_root)
    local_md = workspace / ".claude" / "CLAUDE.local.md"
    has_local_md = local_md.exists()

    candidates = list_session_files(workspace)
    uploaded = read_uploaded(workspace)
    private_ids = read_all_private(workspace)

    # Partition the on-disk transcripts into: private (skip + audit), already
    # uploaded at the same size (skip), and to-upload (new or grown).
    to_upload: list[tuple[str, Path, int]] = []
    private_hits: list[tuple[str, Path]] = []
    skipped_unchanged = 0
    for p in candidates:
        sid = p.stem
        if sid and sid in private_ids:
            private_hits.append((sid, p))
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        prev = uploaded.get(sid)
        if prev is not None and prev == size:
            skipped_unchanged += 1
            continue
        to_upload.append((sid, p, size))

    # ---- DRY RUN ----------------------------------------------------------
    if dry_run:
        plan = {
            "dry_run": True,
            "would_upload": {
                "sessions": [str(p) for _sid, p, _sz in to_upload],
                "local_md": str(local_md) if has_local_md else None,
            },
            "would_skip_private": [{"session_id": sid, "path": str(p)} for sid, p in private_hits],
            "summary": {
                "sessions_count": len(to_upload),
                "private_skipped_count": len(private_hits),
                "skipped_unchanged": skipped_unchanged,
                "local_md_present": has_local_md,
                "uploaded_log": str(uploaded_log_path(workspace)),
            },
        }
        if as_json:
            typer.echo(json.dumps(plan, indent=2))
            return
        if quiet:
            return
        typer.echo(f"Dry run - would upload {len(to_upload)} session file(s)")
        for _sid, p, _sz in to_upload:
            typer.echo(f"  {p}")
        if private_hits:
            typer.echo(f"Would skip {len(private_hits)} private session(s):")
            for sid, p in private_hits:
                typer.echo(f"  [{sid}] {p}")
        if has_local_md:
            typer.echo(f"Would upload CLAUDE.local.md  ({local_md})")
        else:
            typer.echo("No CLAUDE.local.md to upload")
        return

    # ---- REAL RUN ---------------------------------------------------------
    # Acquire single-instance lock. Silent exit if another push already holds
    # it — typical when several SessionEnd hooks fire at once.
    with acquire_or_skip(workspace) as lock:
        if lock is None:
            return  # another push has the lock; this one no-ops

        results: dict = {
            "sessions": 0,
            "local_md": False,
            "errors": [],
            "private_skipped": 0,
            "dropped_permanent": 0,
            "skipped_unchanged": skipped_unchanged,
        }
        now = datetime.now(timezone.utc)

        # Private sessions: never upload, audit-log the intentional skip.
        for sid, p in private_hits:
            mark_private_skipped(workspace, sid, p, now)
            results["private_skipped"] += 1

        for sid, p, size in to_upload:
            ok, info = _upload_one(p)
            if ok:
                results["sessions"] += 1
                # Record immediately (crash-safe): the next push won't re-send.
                mark_uploaded(workspace, sid, size, now)
                continue
            results["errors"].append(info)
            if _is_permanent_failure(info):
                # 4xx (except 401 / 408 / 429): server will never accept it.
                mark_failed_permanent(workspace, sid, p, info["status"], now)
                results["dropped_permanent"] += 1
            # Transient (401 / 408 / 429 / 5xx / network / file-not-found):
            # leave it OUT of the ledger so the next push retries.

        # Upload CLAUDE.local.md from the anchored workspace root.
        if has_local_md:
            try:
                content = local_md.read_text(encoding="utf-8")
                resp = api_post("/api/upload/local-md", json={"content": content})
                if resp.status_code == 200:
                    results["local_md"] = True
                else:
                    results["errors"].append({"file": "CLAUDE.local.md", "status": resp.status_code})
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
    if results["private_skipped"]:
        typer.echo(
            f"Skipped {results['private_skipped']} private session(s) (see .claude/agnes-sessions-private-skipped.txt)"
        )
    if results["dropped_permanent"]:
        typer.echo(
            f"Dropped {results['dropped_permanent']} session(s) with permanent failure "
            f"(see .claude/agnes-sessions-failed.txt)"
        )
    if results["local_md"]:
        typer.echo("Uploaded CLAUDE.local.md")
    if results["errors"]:
        for e in results["errors"]:
            typer.echo(f"warn: {e}", err=True)
