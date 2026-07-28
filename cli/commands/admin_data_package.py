"""`agnes admin data-package` — admin CRUD over Data Packages (v49).

CLI counterpart to the ``/api/admin/data-packages`` surface. Each
subcommand maps 1:1 to one HTTP endpoint:

  - ``list``       → ``GET /api/admin/data-packages``
  - ``create``     → ``POST /api/admin/data-packages``
  - ``edit``       → ``PUT /api/admin/data-packages/{id}``
  - ``delete``     → ``DELETE /api/admin/data-packages/{id}``
  - ``add-table``  → ``POST /api/admin/data-packages/{id}/tables``
  - ``remove-table`` → ``DELETE /api/admin/data-packages/{id}/tables/{table_id}``

Destructive ops require ``--yes`` to skip the confirm prompt — same
pattern as ``agnes admin store push``. ``<pkg>`` arguments accept either
the id or the slug; the wrapper resolves slug→id via a list-and-match
when the supplied value doesn't already look like an id.
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from cli.client import api_get, api_post, api_delete, api_put

admin_data_package_app = typer.Typer(help="Admin: Data Package CRUD")


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    msg = detail if isinstance(detail, str) else (
        json.dumps(detail) if detail is not None else (resp.text or f"HTTP {resp.status_code}")
    )
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


def _resolve_pkg_id(pkg_ref: str) -> str:
    """Accept either an id or a slug. Slugs are resolved via list+match.

    Round-trip cost (one extra GET) is fine — admins do this rarely and
    the alternative (a server lookup-by-slug endpoint) would only exist
    for the CLI's convenience.
    """
    # Heuristic: data_package ids are short opaque tokens, slugs are
    # human-readable. Whatever shape it is, try the GET-by-id first and
    # fall back to slug if 404.
    resp = api_get(f"/api/admin/data-packages/{pkg_ref}")
    if resp.status_code == 200:
        return pkg_ref
    # 404 → try resolving as slug
    resp = api_get("/api/admin/data-packages")
    if resp.status_code != 200:
        _fail(resp)
    for row in resp.json():
        if row.get("slug") == pkg_ref or row.get("id") == pkg_ref:
            return row["id"]
    typer.echo(f"Data package not found: {pkg_ref}", err=True)
    raise typer.Exit(1)


@admin_data_package_app.command("list")
def list_packages(
    search: Optional[str] = typer.Option(None, "--search", help="Name/slug prefix filter"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List all Data Packages."""
    params = {}
    if search:
        params["search"] = search
    resp = api_get("/api/admin/data-packages", params=params or None)
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"Data Packages: {len(rows)}")
    if not rows:
        return
    name_w = max(len("NAME"), max(len(r.get("name", "")) for r in rows))
    slug_w = max(len("SLUG"), max(len(r.get("slug", "")) for r in rows))
    typer.echo(f"{'ID':<14}  {'NAME':<{name_w}}  {'SLUG':<{slug_w}}  DESCRIPTION")
    for r in rows:
        desc = (r.get("description") or "").replace("\n", " ").strip()
        if len(desc) > 50:
            desc = desc[:47] + "..."
        typer.echo(
            f"{r['id']:<14}  {r.get('name',''):<{name_w}}  "
            f"{r.get('slug',''):<{slug_w}}  {desc}"
        )


@admin_data_package_app.command("create")
def create_package(
    name: str = typer.Option(..., "--name", help="Display name"),
    slug: str = typer.Option(..., "--slug", help="URL-safe stable id"),
    description: Optional[str] = typer.Option(None, "--description"),
    icon: Optional[str] = typer.Option(None, "--icon"),
    color: Optional[str] = typer.Option(None, "--color"),
):
    """Create a new Data Package."""
    payload = {"name": name, "slug": slug}
    if description is not None:
        payload["description"] = description
    if icon is not None:
        payload["icon"] = icon
    if color is not None:
        payload["color"] = color
    resp = api_post("/api/admin/data-packages", json=payload)
    if resp.status_code != 201:
        _fail(resp)
    body = resp.json()
    typer.echo(f"Created data_package id={body.get('id')} slug={slug}")


@admin_data_package_app.command("edit")
def edit_package(
    pkg_ref: str = typer.Argument(..., help="Package id or slug"),
    name: Optional[str] = typer.Option(None, "--name"),
    description: Optional[str] = typer.Option(None, "--description"),
    icon: Optional[str] = typer.Option(None, "--icon"),
    color: Optional[str] = typer.Option(None, "--color"),
):
    """Patch Data Package metadata. Only provided fields are updated."""
    payload = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if icon is not None:
        payload["icon"] = icon
    if color is not None:
        payload["color"] = color
    if not payload:
        # Short-circuit BEFORE the slug-resolution roundtrip so callers
        # who pass nothing don't pay for a useless GET.
        typer.echo("Nothing to update. Pass at least one of --name/--description/--icon/--color.", err=True)
        raise typer.Exit(2)
    pkg_id = _resolve_pkg_id(pkg_ref)
    resp = api_put(f"/api/admin/data-packages/{pkg_id}", json=payload)
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Updated data_package {pkg_id}")


@admin_data_package_app.command("delete")
def delete_package(
    pkg_ref: str = typer.Argument(..., help="Package id or slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete a Data Package. Tables in the package are not deleted."""
    pkg_id = _resolve_pkg_id(pkg_ref)
    if not yes:
        confirm = typer.confirm(f"Delete data_package {pkg_id}?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/admin/data-packages/{pkg_id}")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Deleted data_package {pkg_id}")


@admin_data_package_app.command("add-table")
def add_table(
    pkg_ref: str = typer.Argument(..., help="Package id or slug"),
    table_id: str = typer.Argument(..., help="Table id from table_registry"),
):
    """Add a table to the package."""
    pkg_id = _resolve_pkg_id(pkg_ref)
    resp = api_post(
        f"/api/admin/data-packages/{pkg_id}/tables",
        json={"table_id": table_id},
    )
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}
    added = body.get("added", False)
    if added:
        typer.echo(f"Added table {table_id} to data_package {pkg_id}")
    else:
        typer.echo(f"Table {table_id} was already in data_package {pkg_id}")


@admin_data_package_app.command("remove-table")
def remove_table(
    pkg_ref: str = typer.Argument(..., help="Package id or slug"),
    table_id: str = typer.Argument(..., help="Table id from table_registry"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Remove a table from the package."""
    pkg_id = _resolve_pkg_id(pkg_ref)
    if not yes:
        confirm = typer.confirm(
            f"Remove table {table_id} from data_package {pkg_id}?"
        )
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/admin/data-packages/{pkg_id}/tables/{table_id}")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Removed table {table_id} from data_package {pkg_id}")
