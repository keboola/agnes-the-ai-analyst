"""`agnes collections` — manage file corpora (Collections Slice 2).

Commands:
  create  --name ... [--description ...] [--json]
  list    [--json]
  show    <id>       [--json]
  upload    <id> <path...>   (multipart POST per file)
  reingest  <id> <file_id>   (re-run ingestion for one file)
  rm        <id>       [--yes]
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
# search
# ---------------------------------------------------------------------------


@collections_app.command("search")
def search_collections(
    query: str = typer.Argument(..., help="Search query"),
    k: int = typer.Option(10, "--k", help="Max results"),
    collection_id: Optional[str] = typer.Option(None, "--collection", "-c", help="Restrict to one collection id"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Hybrid search across your accessible collections (RBAC-filtered)."""
    params: dict = {"q": query, "k": k}
    if collection_id:
        params["corpus_id"] = collection_id
    try:
        body = api_get_json("/api/collections/search", **params)
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return
    results = body.get("results", [])
    if not results:
        typer.echo("No matches.")
        return
    for r in results:
        loc = r.get("filename") or r.get("file_id")
        typer.echo(f"[{r.get('score')}] {loc} #{r.get('ordinal')}: {(r.get('text') or '')[:120]}")


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
            # api_post_multipart raises on ALL 4xx, INCLUDING 422. The upload
            # endpoint returns 422 with the full per-file result list when some
            # files are rejected — recover it from the error body so the user
            # still sees which files succeeded vs were rejected.
            if exc.status_code == 422 and isinstance(exc.body, list):
                results = exc.body
                any_error = True
            else:
                typer.echo(f"  {fname}: ERROR — {exc}", err=True)
                any_error = True
                continue

        # results is the per-file list (200, or 422 recovered above).
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
# reingest
# ---------------------------------------------------------------------------


@collections_app.command("reingest")
def reingest_file(
    collection_id: str = typer.Argument(..., help="Collection id (col_...)"),
    file_id: str = typer.Argument(..., help="File id (cf_...) from `collections show`"),
):
    """Re-run ingestion for one file (admin; after fixing the file or config)."""
    try:
        out = api_post_json(f"/api/collections/{collection_id}/files/{file_id}/reingest", {})
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    typer.echo(f"reingest queued: {out.get('file_id', file_id)} status={out.get('processing_status', '?')}")


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
