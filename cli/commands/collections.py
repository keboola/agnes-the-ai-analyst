"""`agnes collections` — manage file corpora (Collections Slice 2).

Commands:
  create  --name ... [--description ...] [--json]
  list    [--json]
  show    <id>       [--json]
  upload  <id> <path...>   (multipart POST per file)
  rm      <id>       [--yes]
"""

from __future__ import annotations

import json as json_lib
from pathlib import Path
from typing import Optional

import typer

from cli.v2_client import (
    V2ClientError,
    api_delete,
    api_get_json,
    api_post_json,
    api_post_multipart,
)

collections_app = typer.Typer(help="Manage file collections (upload corpora)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_collection(col: dict) -> str:
    desc = col.get("description") or ""
    return f"{col['id']:20s}  {col.get('slug', ''):20s}  {col['name']}  {desc}"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@collections_app.command("create")
def create_collection(
    name: str = typer.Option(..., "--name", help="Collection name"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Description"),
    slug: Optional[str] = typer.Option(None, "--slug", help="URL-safe slug (auto-generated if omitted)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Create a new file collection (admin only)."""
    payload: dict = {"name": name}
    if description is not None:
        payload["description"] = description
    if slug is not None:
        payload["slug"] = slug
    try:
        body = api_post_json("/api/collections", payload)
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return
    typer.echo(f"Created: id={body['id']}  slug={body.get('slug', '')}  name={body['name']}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@collections_app.command("list")
def list_collections(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """List collections accessible to you (RBAC-filtered)."""
    try:
        body = api_get_json("/api/collections")
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    items = body.get("items", [])
    if not items:
        typer.echo("No collections found.")
        return
    typer.echo(f"{'ID':20s}  {'SLUG':20s}  NAME")
    for col in items:
        typer.echo(_fmt_collection(col))


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@collections_app.command("show")
def show_collection(
    collection_id: str = typer.Argument(..., help="Collection ID (e.g. col_abc123)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show detail + file list for a collection."""
    try:
        body = api_get_json(f"/api/collections/{collection_id}")
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(f"ID:          {body['id']}")
    typer.echo(f"Slug:        {body.get('slug', '')}")
    typer.echo(f"Name:        {body['name']}")
    if body.get("description"):
        typer.echo(f"Description: {body['description']}")
    typer.echo(f"Created by:  {body.get('created_by', '')}")
    files = body.get("files", [])
    typer.echo(f"\nFiles ({len(files)}):")
    if not files:
        typer.echo("  (none)")
        return
    typer.echo(f"  {'FILE_ID':20s}  {'STATUS':10s}  {'SIZE':8s}  FILENAME")
    for f in files:
        size = f.get("size_bytes") or 0
        typer.echo(f"  {f['file_id']:20s}  {f['processing_status']:10s}  {size:8d}  {f['filename']}")


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


@collections_app.command("upload")
def upload_files(
    collection_id: str = typer.Argument(..., help="Collection ID"),
    paths: list[Path] = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="One or more local file paths to upload",
    ),
):
    """Upload one or more files into a collection (multipart POST per file).

    Each file is sent as a separate request.  The server's extension
    allowlist determines whether a file lands as ``pending`` (tier1/tier2)
    or ``rejected`` (unsupported type).
    """
    any_error = False
    for path in paths:
        fname = path.name
        file_bytes = path.read_bytes()
        files = {
            "files": (fname, file_bytes, "application/octet-stream"),
        }
        try:
            results = api_post_multipart(f"/api/collections/{collection_id}/files", files=files)
        except V2ClientError as exc:
            typer.echo(f"  {fname}: ERROR — {exc}", err=True)
            any_error = True
            continue

        # results may be a list (200) or a list via 422 — api_post_multipart
        # raises on 4xx (non-422). Handle both list and dict shapes.
        if isinstance(results, list):
            for row in results:
                status = row.get("processing_status", "?")
                fid = row.get("file_id", "?")
                typer.echo(f"  {row.get('filename', fname)}: {status} (file_id={fid})")
                if status == "rejected":
                    any_error = True
        else:
            typer.echo(str(results))

    if any_error:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


@collections_app.command("rm")
def remove_collection(
    collection_id: str = typer.Argument(..., help="Collection ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Soft-delete a collection (admin only)."""
    if not yes:
        confirmed = typer.confirm(f"Delete collection {collection_id}?")
        if not confirmed:
            raise typer.Abort()
    try:
        api_delete(f"/api/collections/{collection_id}")
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"Deleted: {collection_id}")
