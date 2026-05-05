"""`agnes store {list,show,install,uninstall,upload,delete}` — community
marketplace browse/install over the REST API.

Mirrors the /store web UI for analyst CLI workflows. Listing + filters are
the read paths; install/uninstall/upload/delete are the write paths. All
commands authenticate via the configured PAT (see ``cli auth``); the
endpoints are gated by ``get_current_user`` server-side.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from cli.v2_client import (
    V2ClientError,
    api_delete,
    api_get_json,
    api_get_stream,
    api_post_json,
    api_post_multipart,
    api_put_multipart,
)

store_app = typer.Typer(help="Community Store — browse, install, upload skills/agents/plugins")


@store_app.command("list")
def list_entities(
    type: Optional[str] = typer.Option(None, "--type", help="skill | agent | plugin"),
    category: Optional[str] = typer.Option(None, "--category"),
    search: Optional[str] = typer.Option(None, "--search", "-q"),
    owner: Optional[str] = typer.Option(None, "--owner", help="Filter by owner user_id"),
    limit: int = typer.Option(24, "--limit", min=1, max=100),
    skip: int = typer.Option(0, "--skip", min=0),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table"),
):
    """List Store entities with optional filters."""
    params: dict = {"limit": limit, "skip": skip}
    if type:
        params["type"] = type
    if category:
        params["category"] = category
    if search:
        params["search"] = search
    if owner:
        params["owner"] = owner
    try:
        body = api_get_json("/api/store/entities", **params)
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    if json_out:
        typer.echo(json.dumps(body, indent=2))
        return
    items = body.get("items", [])
    total = body.get("total", 0)
    typer.echo(f"{total} entit(y) total — showing {len(items)} (skip={skip}):")
    for it in items:
        typer.echo(
            f"  [{it['type']:6s}] {it['name']:24s} by {it['owner_username']:20s} "
            f"installs={it['install_count']:<4d} v{it['version']}  id={it['id']}"
        )


@store_app.command("show")
def show_entity(
    entity_id: str = typer.Argument(...),
    json_out: bool = typer.Option(False, "--json"),
):
    """Show a Store entity's full metadata."""
    try:
        body = api_get_json(f"/api/store/entities/{entity_id}")
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    if json_out:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"{body['name']} ({body['type']}) v{body['version']}")
    typer.echo(f"  by {body['owner_username']} ({body.get('owner_display_name') or '?'})")
    typer.echo(f"  invocation: {body['invocation_name']}")
    if body.get("description"):
        typer.echo(f"  description: {body['description']}")
    typer.echo(f"  installs: {body['install_count']}, size: {body['file_size']} bytes")
    if body.get("video_url"):
        typer.echo(f"  video: {body['video_url']}")


@store_app.command("install")
def install_entity(entity_id: str = typer.Argument(...)):
    """Install a Store entity into your `/marketplace.zip` view."""
    try:
        body = api_post_json(f"/api/store/entities/{entity_id}/install", {})
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Installed: entity_id={body['entity_id']}")


@store_app.command("uninstall")
def uninstall_entity(entity_id: str = typer.Argument(...)):
    """Uninstall a Store entity from your view."""
    try:
        body = api_delete(f"/api/store/entities/{entity_id}/install")
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Uninstalled: entity_id={body.get('entity_id', entity_id)}")


@store_app.command("upload")
def upload_entity(
    type: str = typer.Argument(..., help="skill | agent | plugin"),
    zip_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    name: Optional[str] = typer.Option(None, "--name"),
    description: Optional[str] = typer.Option(None, "--description"),
    category: Optional[str] = typer.Option(None, "--category"),
    video_url: Optional[str] = typer.Option(None, "--video-url"),
):
    """Upload a Store entity from a local ZIP file."""
    files = {
        "file": (zip_path.name, zip_path.read_bytes(), "application/zip"),
    }
    data: dict = {"type": type}
    if name:
        data["name"] = name
    if description:
        data["description"] = description
    if category:
        data["category"] = category
    if video_url:
        data["video_url"] = video_url
    try:
        body = api_post_multipart("/api/store/entities", files=files, data=data)
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(
        f"Uploaded: id={body['id']} name={body['name']} "
        f"invocation={body['invocation_name']} version={body['version']}"
    )


@store_app.command("delete")
def delete_entity(
    entity_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete a Store entity (owner or admin only)."""
    if not yes:
        confirm = typer.confirm(f"Delete entity {entity_id}?")
        if not confirm:
            raise typer.Abort()
    try:
        api_delete(f"/api/store/entities/{entity_id}")
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Deleted: {entity_id}")


@store_app.command("update")
def update_entity(
    entity_id: str = typer.Argument(...),
    description: Optional[str] = typer.Option(None, "--description"),
    category: Optional[str] = typer.Option(None, "--category"),
    video_url: Optional[str] = typer.Option(None, "--video-url"),
    photo: Optional[Path] = typer.Option(
        None, "--photo", exists=True, dir_okay=False, readable=True,
        help="Replace the entity's photo with this image file",
    ),
    zip_path: Optional[Path] = typer.Option(
        None, "--zip", exists=True, dir_okay=False, readable=True,
        help="Replace the plugin tree with this new ZIP",
    ),
):
    """In-place edit a Store entity. Owner or admin only.

    Server-side authorization (PUT /api/store/entities/{id}) admits the
    owner OR any member of the Admin group; CLI doesn't enforce, the
    server does. Pass any combination of --description / --category /
    --video-url / --photo / --zip; omitted fields are left untouched
    (note: an empty string clears nothing — there's no API affordance to
    clear a field back to NULL via PUT today).
    """
    files: dict = {}
    data: dict = {}
    if zip_path:
        files["file"] = (zip_path.name, zip_path.read_bytes(), "application/zip")
    if photo:
        files["photo"] = (photo.name, photo.read_bytes(), f"image/{photo.suffix.lstrip('.')}")
    if description is not None:
        data["description"] = description
    if category is not None:
        data["category"] = category
    if video_url is not None:
        data["video_url"] = video_url
    if not files and not data:
        typer.echo("Nothing to update — pass at least one of --description / --category / --video-url / --photo / --zip.", err=True)
        raise typer.Exit(2)
    try:
        body = api_put_multipart(
            f"/api/store/entities/{entity_id}",
            files=files or None, data=data,
        )
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(
        f"Updated: id={body['id']} version={body['version']}"
    )


# ---------------------------------------------------------------------------
# Bundle: pull + info (read paths, any authenticated user).
# Bulk restore (push) lives under `agnes admin store push` because the
# server-side endpoint is admin-only.
# ---------------------------------------------------------------------------


@store_app.command("pull")
def pull_bundle(
    type: Optional[str] = typer.Option(None, "--type", help="skill | agent | plugin"),
    category: Optional[str] = typer.Option(None, "--category"),
    owner: Optional[str] = typer.Option(None, "--owner", help="Filter by owner user_id"),
    search: Optional[str] = typer.Option(None, "--search", "-q"),
    out: Path = typer.Option(
        Path("agnes-store-bundle.zip"), "-o", "--out",
        help="Where to save the ZIP (default: ./agnes-store-bundle.zip)",
    ),
    unpack: Optional[Path] = typer.Option(
        None, "--unpack",
        help="Instead of saving the ZIP, unpack it into this directory. "
             "Useful for committing a snapshot to a backup git repo: "
             "`agnes store pull --unpack ./backup/ && cd backup && git add .`",
    ),
):
    """Download the whole Store as a deterministic ZIP.

    With ``--unpack DIR`` the ZIP is streamed and immediately extracted
    into ``DIR`` (the directory is wiped first so re-runs leave a clean
    diff). The bundle layout::

        manifest.json
        entities/<entity_id>/
        ├── plugin/...
        └── assets/...

    Every entity matching the given filters is included; no filters =
    everything in the Store.
    """
    import shutil as _shutil
    import tempfile as _tempfile
    import zipfile as _zipfile

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
        # Stream into a temp file, then unpack into `unpack` (wiped first).
        scratch = Path(_tempfile.mkdtemp(prefix="agnes_store_pull_"))
        zip_path = scratch / "bundle.zip"
        try:
            try:
                api_get_stream("/api/store/bundle.zip", str(zip_path), **params)
            except V2ClientError as e:
                typer.echo(str(e), err=True)
                raise typer.Exit(1)
            if unpack.exists():
                _shutil.rmtree(unpack)
            unpack.mkdir(parents=True, exist_ok=True)
            with _zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(unpack)
        finally:
            _shutil.rmtree(scratch, ignore_errors=True)
        typer.echo(f"Unpacked Store bundle → {unpack}")
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = api_get_stream("/api/store/bundle.zip", str(out), **params)
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Wrote {size:,} bytes → {out}")


@store_app.command("info")
def store_info(
    json_out: bool = typer.Option(False, "--json"),
):
    """Summary of the Store: total entities, breakdown by type, total size.

    No new endpoint — assembled client-side from a paginated /entities
    sweep so it stays in sync with what `pull` would emit.
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
