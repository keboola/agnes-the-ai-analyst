"""`agnes admin store push` — admin-only Store bulk restore.

Wraps ``POST /api/store/import-bundle`` (admin-gated). Read paths
(``pull`` / ``info``) live under user-namespace ``agnes store`` because the
server endpoint for the export is open to any authenticated user (the
Store is community-readable).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import typer

from cli.v2_client import V2ClientError, api_post_multipart

admin_store_app = typer.Typer(help="Admin: Store bulk restore (push)")


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
