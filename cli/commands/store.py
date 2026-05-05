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
    api_post_json,
    api_post_multipart,
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
