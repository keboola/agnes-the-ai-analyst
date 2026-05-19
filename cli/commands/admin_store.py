"""`agnes admin store {pull,push,info}` — operator-flavored bulk Store ops.

Read direction (``pull`` / ``info``) lives here too even though the server
endpoint is open to any authenticated user, so all backup-orchestration
commands sit in one namespace. Analyst-facing per-entity browse stays in
``agnes store``; analysts who want to download just their OWN uploads
have ``agnes store mine``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import typer

from cli.v2_client import (
    V2ClientError,
    api_get_json,
    api_get_stream,
    api_post_multipart,
)

admin_store_app = typer.Typer(help="Admin: bulk Store ops (pull / push / info)")


@admin_store_app.command("pull")
def pull_bundle(
    type: Optional[str] = typer.Option(None, "--type", help="skill | agent | plugin"),
    category: Optional[str] = typer.Option(None, "--category"),
    owner: Optional[str] = typer.Option(None, "--owner", help="Filter by owner user_id"),
    search: Optional[str] = typer.Option(None, "--search", "-q"),
    out: Path = typer.Option(
        Path("flea.zip"), "-o", "--out",
        help="Where to save the ZIP (default: ./flea.zip)",
    ),
    unpack: Optional[Path] = typer.Option(
        None, "--unpack",
        help="Instead of saving the ZIP, unpack it into this directory. "
             "Useful for committing a snapshot to a backup git repo: "
             "`agnes admin store pull --unpack ./backup/ && cd backup && git add .`",
    ),
):
    """Download the whole Store as a deterministic ZIP.

    With ``--unpack DIR`` the ZIP is streamed and immediately extracted
    into ``DIR`` (the directory is wiped first so re-runs leave a clean
    diff). Bundle layout::

        manifest.json
        entities/<entity_id>/
        ├── plugin/...
        └── assets/...

    Every entity matching the given filters is included; no filters =
    everything in the Store. Server endpoint is open (any authenticated
    user can call it) — this command lives under ``admin store`` only by
    operational convention; analysts wanting their OWN uploads use
    ``agnes store mine``.
    """
    params: dict = {}
    if type:
        params["type"] = type
    if category:
        params["category"] = category
    if owner:
        params["owner"] = owner
    if search:
        params["search"] = search

    if unpack:
        scratch = Path(tempfile.mkdtemp(prefix="agnes_store_pull_"))
        zip_path = scratch / "bundle.zip"
        try:
            try:
                api_get_stream("/api/store/bundle.zip", str(zip_path), **params)
            except V2ClientError as e:
                typer.echo(str(e), err=True)
                raise typer.Exit(1)
            if unpack.exists():
                shutil.rmtree(unpack)
            unpack.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(unpack)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        typer.echo(f"Unpacked Store bundle → {unpack}")
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = api_get_stream("/api/store/bundle.zip", str(out), **params)
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Wrote {size:,} bytes → {out}")


@admin_store_app.command("info")
def store_info(
    json_out: bool = typer.Option(False, "--json"),
):
    """Summary of the Store: total entities, breakdown by type, total size.

    Assembled client-side from a paginated /entities sweep so it stays
    in sync with what `pull` would emit.
    """
    skip = 0
    page = 100
    by_type: dict = {}
    total_entities = 0
    total_size = 0
    while True:
        try:
            body = api_get_json(
                "/api/store/entities", limit=page, skip=skip,
            )
        except V2ClientError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1)
        items = body.get("items", [])
        if not items:
            break
        for it in items:
            total_entities += 1
            total_size += int(it.get("file_size") or 0)
            by_type[it["type"]] = by_type.get(it["type"], 0) + 1
        if len(items) < page:
            break
        skip += page

    summary = {
        "total_entities": total_entities,
        "total_file_size_bytes": total_size,
        "by_type": by_type,
    }
    if json_out:
        typer.echo(json.dumps(summary, indent=2))
        return
    typer.echo(f"Store: {total_entities} entit, {total_size:,} bytes total")
    for t in sorted(by_type):
        typer.echo(f"  {t:8s} {by_type[t]}")


@admin_store_app.command("push")
def push_bundle(
    source: Path = typer.Argument(
        ..., exists=True, readable=True,
        help="Bundle to upload — either a *.zip file or a directory "
             "containing manifest.json + entities/. A directory is "
             "zipped client-side before upload.",
    ),
    mode: str = typer.Option(
        "merge", "--mode",
        help="merge (default — upsert by entity_id; replace when version "
             "differs) | replace (overwrite every existing row in the "
             "bundle) | skip (insert only entities not already present)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Upload a Store bundle ZIP for bulk restore. Admin only."""
    if mode not in {"merge", "replace", "skip"}:
        typer.echo(f"--mode must be merge|replace|skip, got {mode!r}", err=True)
        raise typer.Exit(2)

    # If source is a directory, zip it client-side. The expected layout is
    # the same as `agnes store pull --unpack` produces: manifest.json at
    # the top, entities/<id>/ subtrees.
    cleanup: Optional[Path] = None
    try:
        if source.is_dir():
            if not (source / "manifest.json").is_file():
                typer.echo(
                    f"{source} does not contain manifest.json — is this a Store bundle directory?",
                    err=True,
                )
                raise typer.Exit(2)
            scratch = Path(tempfile.mkdtemp(prefix="agnes_store_push_"))
            cleanup = scratch
            zip_path = scratch / "bundle.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(p for p in source.rglob("*") if p.is_file()):
                    rel = f.relative_to(source).as_posix()
                    zf.write(f, arcname=rel)
            zip_to_send = zip_path
        else:
            zip_to_send = source

        if not yes:
            confirm = typer.confirm(
                f"Upload bundle from {source} with mode={mode}? "
                f"This may modify existing Store entities."
            )
            if not confirm:
                raise typer.Abort()

        files = {
            "file": (zip_to_send.name, zip_to_send.read_bytes(), "application/zip"),
        }
        try:
            body = api_post_multipart(
                "/api/store/import-bundle",
                files=files, data={"mode": mode},
            )
        except V2ClientError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1)
        typer.echo(
            f"imported={body.get('imported', 0)} "
            f"replaced={body.get('replaced', 0)} "
            f"skipped={body.get('skipped', 0)} "
            f"stub_users_created={body.get('stub_users_created', 0)}"
        )
        errs = body.get("errors") or []
        if errs:
            typer.echo(f"\n{len(errs)} entries had errors:", err=True)
            for e in errs[:10]:
                typer.echo(f"  - {json.dumps(e)}", err=True)
            if len(errs) > 10:
                typer.echo(f"  ... and {len(errs) - 10} more", err=True)
            raise typer.Exit(1)
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)
