"""`agnes push` — upload session jsonls + CLAUDE.local.md to the server.

The push command consumes a workspace-local queue file
(``<workspace>/.claude/agnes-sessions.txt``) populated by the
``agnes capture-session`` SessionStart + SessionEnd hooks. Each line is
a TSV row: ``<session_id>\\t<transcript_path>[\\t<first_failed_iso>]``.
The session_id lets push consult the private list
(``cli/lib/private_list.py``) and skip uploads that the user explicitly
marked via ``/agnes-private``. The optional third column stamps the
first failed upload attempt; failing entries are requeued (not dropped)
until ``RETRY_TTL`` elapses, then moved to the forensic failed-log.

Concurrency: a single-instance lock (``filelock`` via ``cli/lib/push_lock.py``)
ensures only one push runs at a time. When the user closes several Claude
Code sessions simultaneously, every SessionEnd hook fires its own
``agnes push``; exactly one runs, the rest exit silently.

Race protection: the queue file is atomically renamed to a snapshot before
processing. SessionStart hooks that fire during the push window write to
a freshly-created queue, so their entries aren't lost.

Recovery: if a previous push crashed mid-run, its snapshot file persists.
The next push picks it up before processing the current queue.

Private filter: even if a marked-private session_id slipped into the
queue before ``/agnes-private`` was run, push re-checks the private list
per entry. Skipped entries are audit-logged to
``<workspace>/.claude/agnes-sessions-private-skipped.txt``.

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
from cli.lib.private_list import read_all_private
from cli.lib.push_lock import acquire_or_skip
from cli.lib.session_queue import (
    discard_snapshot,
    find_recovery_snapshots,
    mark_failed_permanent,
    mark_private_skipped,
    mark_uploaded,
    queue_path,
    read_entries_from_snapshot,
    requeue_failed,
    retry_expired,
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


def _gather_entries_for_dry_run(workspace: Path) -> list[tuple[str, Path]]:
    """Read (session_id, path) entries that *would* be uploaded without
    consuming the queue. Combines existing recovery snapshots + the
    current live queue. Does NOT rename the live queue (dry-run is
    read-only). The retry stamp is bookkeeping, not display — dropped here.
    """
    out: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()

    def _add(entries: list[tuple[str, Path, str]]) -> None:
        for sid, p, _stamp in entries:
            key = (sid, str(p))
            if key in seen:
                continue
            seen.add(key)
            out.append((sid, p))

    for snap in find_recovery_snapshots(workspace):
        _add(read_entries_from_snapshot(snap))

    live = queue_path(workspace)
    if live.exists():
        _add(read_entries_from_snapshot(live))

    return out


def _is_permanent_failure(info: dict) -> bool:
    """True iff the server's response indicates a deterministic failure
    that retrying won't help. We treat 4xx (except 401 / 408 / 429) as
    permanent — 403 (RBAC denial), 413 (payload too large), 400
    (validation error) all have the same property: re-uploading the
    same file produces the same answer, so a requeue-loop only wastes
    bytes and grows the queue forever. 5xx and network exceptions stay
    transient — those reflect server or transport state that can change
    between push runs.

    401 is RECOVERABLE, not permanent: it means the PAT expired (90-day
    TTL) or hasn't been imported yet. The user re-authenticates and the
    same upload succeeds — dropping the queue on 401 silently lost every
    session pushed between token expiry and the next `agnes auth` run.
    The retry-stamp TTL (``RETRY_TTL``) bounds the requeue window, so
    this cannot regress into the old infinite-requeue bug.

    408 Request Timeout and 429 Too Many Requests are flagged transient
    by the HTTP spec (RFC 7231 / RFC 6585); the server is telling us
    to back off and try again later, not that the request is invalid.
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
    legacy_scan: bool = typer.Option(
        False,
        "--legacy-scan",
        help=(
            "Fallback: also include sessions found by the encoding-based scan "
            "of ~/.claude/projects/. Use for one-off backfill of sessions "
            "predating the queue mechanism. The /agnes-private list IS "
            "consulted — Claude Code names jsonls ``<session-id>.jsonl`` so "
            "the file stem provides the session id even for legacy entries."
        ),
    ),
):
    """Upload queued session jsonls + CLAUDE.local.md from this workspace."""
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

    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()
    local_md = workspace / ".claude" / "CLAUDE.local.md"
    has_local_md = local_md.exists()

    # ---- DRY RUN ----------------------------------------------------------
    if dry_run:
        candidates = _gather_entries_for_dry_run(workspace)
        private_ids = read_all_private(workspace)
        non_private = [(sid, p) for sid, p in candidates if not (sid and sid in private_ids)]
        private_skipped = [(sid, p) for sid, p in candidates if sid and sid in private_ids]

        if legacy_scan:
            from cli.lib.claude_sessions import list_session_files

            seen = {str(p) for _sid, p in non_private}
            for p in list_session_files(workspace):
                if str(p) in seen:
                    continue
                # Apply the private filter to legacy-scan candidates too.
                # Claude Code names jsonls ``<session-id>.jsonl``, so the
                # file stem IS the session id and we can apply the same
                # filter the queue path uses. Closes the gap David #8
                # raised: legacy-scan would otherwise upload everything
                # on disk, including sessions the user later marked
                # private.
                sid_from_path = p.stem
                if sid_from_path and sid_from_path in private_ids:
                    private_skipped.append((sid_from_path, p))
                else:
                    non_private.append((sid_from_path, p))
                seen.add(str(p))

        plan = {
            "dry_run": True,
            "would_upload": {
                "sessions": [str(p) for _sid, p in non_private],
                "local_md": str(local_md) if has_local_md else None,
            },
            "would_skip_private": [{"session_id": sid, "path": str(p)} for sid, p in private_skipped],
            "summary": {
                "sessions_count": len(non_private),
                "private_skipped_count": len(private_skipped),
                "local_md_present": has_local_md,
                "uploaded_log": str(uploaded_log_path(workspace)),
            },
        }
        if as_json:
            typer.echo(json.dumps(plan, indent=2))
            return
        if quiet:
            return
        typer.echo(f"Dry run - would upload {len(non_private)} session file(s)")
        for _sid, p in non_private:
            typer.echo(f"  {p}")
        if private_skipped:
            typer.echo(f"Would skip {len(private_skipped)} private session(s):")
            for sid, p in private_skipped:
                typer.echo(f"  [{sid}] {p}")
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

        results = {
            "sessions": 0,
            "local_md": False,
            "errors": [],
            "private_skipped": 0,
            "dropped_permanent": 0,
            "requeued": 0,
        }

        # Snapshot the private list once at the start of the run. Adding
        # a new private ID between snapshot and the per-entry check is
        # benign (worst case: one more upload of a session the user just
        # marked, which next push will skip).
        private_ids = read_all_private(workspace)

        # Process snapshots: recovery (from prior crash) first, then fresh.
        snapshots = _collect_snapshots(workspace)
        all_failed_entries: list[tuple[str, Path, str]] = []

        for snapshot in snapshots:
            entries = read_entries_from_snapshot(snapshot)
            failed_in_snapshot: list[tuple[str, Path, str]] = []
            now = datetime.now(timezone.utc)
            now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            for session_id, transcript, first_failed in entries:
                if session_id and session_id in private_ids:
                    # Skip private; audit-log and move on. Do not requeue —
                    # this is the user's explicit "do not upload" intent.
                    mark_private_skipped(workspace, session_id, transcript, now)
                    results["private_skipped"] += 1
                    continue
                ok, info = _upload_one(transcript)
                if ok:
                    results["sessions"] += 1
                    mark_uploaded(workspace, transcript, now)
                    continue
                # Failure handling. Everything non-permanent is requeued
                # with a first-failure stamp; RETRY_TTL bounds the window.
                #
                # "file not found on disk" is NOT a permanent condition:
                # Claude Code creates the transcript lazily on the first
                # prompt, so an entry captured at SessionStart routinely
                # points at a file that doesn't exist YET. Dropping it
                # here (the pre-stamp behavior) permanently lost any
                # session whose start raced another session's push.
                # Genuinely deleted transcripts age out via the TTL.
                if _is_permanent_failure(info):
                    # 4xx (except 401 / 408 / 429): server says this
                    # request will never succeed. Drop + audit-log
                    # instead of requeueing forever.
                    mark_failed_permanent(
                        workspace,
                        session_id,
                        transcript,
                        info["status"],
                        now,
                    )
                    results["dropped_permanent"] += 1
                    results["errors"].append(info)
                elif retry_expired(first_failed, now):
                    # Failing for longer than RETRY_TTL — transcript gone
                    # for good, or the server persistently rejecting it.
                    # Move to the forensic log so the queue stays bounded.
                    reason = info.get("status") or "retry_expired"
                    if info.get("error") == "file not found on disk":
                        reason = "not_found_expired"
                    mark_failed_permanent(
                        workspace,
                        session_id,
                        transcript,
                        reason,
                        now,
                    )
                    results["dropped_permanent"] += 1
                    results["errors"].append(info)
                else:
                    # Transient (missing-file, 401, 5xx, 408, 429,
                    # network errors): requeue for the next push,
                    # stamping the first failure time on first failure.
                    results["errors"].append(info)
                    results["requeued"] += 1
                    failed_in_snapshot.append((session_id, transcript, first_failed or now_iso))
            # Failed entries from this snapshot get re-queued on the live file.
            all_failed_entries.extend(failed_in_snapshot)
            discard_snapshot(snapshot)

        if all_failed_entries:
            requeue_failed(workspace, all_failed_entries)

        # Optional: legacy scan to backfill sessions outside the queue.
        # Honors the private list — Claude Code names jsonls
        # ``<session-id>.jsonl``, so the file stem IS the session id and
        # we can apply the same filter the queue path uses. Without this
        # filter, an operator running ``--legacy-scan`` to backfill old
        # sessions would silently upload every transcript on disk,
        # including ones the user later marked private (David's #8 from
        # the PR review).
        if legacy_scan:
            from cli.lib.claude_sessions import list_session_files

            private_ids = read_all_private(workspace)
            for transcript in list_session_files(workspace):
                sid_from_path = transcript.stem
                if sid_from_path and sid_from_path in private_ids:
                    mark_private_skipped(workspace, sid_from_path, str(transcript))
                    results["private_skipped"] = results.get("private_skipped", 0) + 1
                    continue
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
    if results["requeued"]:
        typer.echo(
            f"Requeued {results['requeued']} session(s) for the next push "
            f"(transcript not written yet, auth expired, or server unavailable)"
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
