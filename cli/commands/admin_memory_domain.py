"""`agnes admin memory-domain` — admin CRUD over Memory Domains (v49).

Mirrors ``cli/commands/admin_data_package.py``. Each subcommand maps
1:1 to a ``/api/admin/memory-domains`` endpoint:

  - ``list``       → ``GET /api/admin/memory-domains``
  - ``create``     → ``POST /api/admin/memory-domains``
  - ``edit``       → ``PUT /api/admin/memory-domains/{id}``
  - ``delete``     → ``DELETE /api/admin/memory-domains/{id}``
  - ``add-item``   → ``POST /api/admin/memory-domains/{id}/items``
  - ``remove-item`` → ``DELETE /api/admin/memory-domains/{id}/items/{item_id}``

Same ``--yes`` confirmation pattern as Data Package destructive ops.
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from cli.client import api_get, api_post, api_delete, api_put

admin_memory_domain_app = typer.Typer(help="Admin: Memory Domain CRUD")


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


def _resolve_domain_id(domain_ref: str) -> str:
    """Accept either id or slug. Same lookup pattern as
    ``admin_data_package._resolve_pkg_id``."""
    resp = api_get(f"/api/admin/memory-domains/{domain_ref}")
    if resp.status_code == 200:
        return domain_ref
    resp = api_get("/api/admin/memory-domains")
    if resp.status_code != 200:
        _fail(resp)
    for row in resp.json():
        if row.get("slug") == domain_ref or row.get("id") == domain_ref:
            return row["id"]
    typer.echo(f"Memory domain not found: {domain_ref}", err=True)
    raise typer.Exit(1)


@admin_memory_domain_app.command("list")
def list_domains(
    search: Optional[str] = typer.Option(None, "--search"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List all Memory Domains."""
    params = {}
    if search:
        params["search"] = search
    resp = api_get("/api/admin/memory-domains", params=params or None)
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"Memory Domains: {len(rows)}")
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


@admin_memory_domain_app.command("create")
def create_domain(
    name: str = typer.Option(..., "--name"),
    slug: str = typer.Option(..., "--slug"),
    description: Optional[str] = typer.Option(None, "--description"),
    icon: Optional[str] = typer.Option(None, "--icon"),
    color: Optional[str] = typer.Option(None, "--color"),
):
    """Create a new Memory Domain."""
    payload = {"name": name, "slug": slug}
    if description is not None:
        payload["description"] = description
    if icon is not None:
        payload["icon"] = icon
    if color is not None:
        payload["color"] = color
    resp = api_post("/api/admin/memory-domains", json=payload)
    if resp.status_code != 201:
        _fail(resp)
    body = resp.json()
    typer.echo(f"Created memory_domain id={body.get('id')} slug={slug}")


@admin_memory_domain_app.command("edit")
def edit_domain(
    domain_ref: str = typer.Argument(..., help="Domain id or slug"),
    name: Optional[str] = typer.Option(None, "--name"),
    description: Optional[str] = typer.Option(None, "--description"),
    icon: Optional[str] = typer.Option(None, "--icon"),
    color: Optional[str] = typer.Option(None, "--color"),
):
    """Patch Memory Domain metadata."""
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
        typer.echo(
            "Nothing to update. Pass at least one of --name/--description/--icon/--color.",
            err=True,
        )
        raise typer.Exit(2)
    domain_id = _resolve_domain_id(domain_ref)
    resp = api_put(f"/api/admin/memory-domains/{domain_id}", json=payload)
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Updated memory_domain {domain_id}")


@admin_memory_domain_app.command("delete")
def delete_domain(
    domain_ref: str = typer.Argument(..., help="Domain id or slug"),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Delete a Memory Domain. Items are not deleted; only the
    domain-membership junction is cleared."""
    domain_id = _resolve_domain_id(domain_ref)
    if not yes:
        confirm = typer.confirm(f"Delete memory_domain {domain_id}?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/admin/memory-domains/{domain_id}")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Deleted memory_domain {domain_id}")


@admin_memory_domain_app.command("add-item")
def add_item(
    domain_ref: str = typer.Argument(..., help="Domain id or slug"),
    item_id: str = typer.Argument(..., help="Knowledge item id"),
):
    """Tag a knowledge item with this domain."""
    domain_id = _resolve_domain_id(domain_ref)
    resp = api_post(
        f"/api/admin/memory-domains/{domain_id}/items",
        json={"item_id": item_id},
    )
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}
    added = body.get("added", False)
    if added:
        typer.echo(f"Added item {item_id} to memory_domain {domain_id}")
    else:
        typer.echo(f"Item {item_id} was already in memory_domain {domain_id}")


@admin_memory_domain_app.command("remove-item")
def remove_item(
    domain_ref: str = typer.Argument(..., help="Domain id or slug"),
    item_id: str = typer.Argument(..., help="Knowledge item id"),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Untag a knowledge item from this domain."""
    domain_id = _resolve_domain_id(domain_ref)
    if not yes:
        confirm = typer.confirm(
            f"Remove item {item_id} from memory_domain {domain_id}?"
        )
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/admin/memory-domains/{domain_id}/items/{item_id}")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Removed item {item_id} from memory_domain {domain_id}")
