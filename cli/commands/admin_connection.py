"""`agnes admin connection` — manage named source connections (multi-project Keboola).

CLI counterpart to the ``/api/admin/source-connections`` surface.
Each subcommand maps 1:1 to one HTTP endpoint:

  - ``list``    → ``GET /api/admin/source-connections``
  - ``add``     → ``POST /api/admin/source-connections`` + ``PUT /{id}/secret``
  - ``remove``  → ``DELETE /api/admin/source-connections/{id}``
  - ``test``    → ``POST /api/admin/source-connections/{id}/test``
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_post, api_put

admin_connection_app = typer.Typer(help="Admin: named source-connection CRUD")


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    msg = (
        detail
        if isinstance(detail, str)
        else (json.dumps(detail) if detail is not None else (resp.text or f"HTTP {resp.status_code}"))
    )
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


@admin_connection_app.command("list")
def list_connections(
    source_type: Optional[str] = typer.Option(None, "--source-type", help="Filter by source type (e.g. keboola)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List named source connections."""
    params = {}
    if source_type:
        params["source_type"] = source_type
    resp = api_get("/api/admin/source-connections", params=params or None)
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No connections.")
        return
    name_w = max(len("NAME"), max(len(r.get("name", "")) for r in rows))
    type_w = max(len("TYPE"), max(len(r.get("source_type", "")) for r in rows))
    typer.echo(f"{'ID':<26}  {'NAME':<{name_w}}  {'TYPE':<{type_w}}  DEFAULT  STACK URL")
    for r in rows:
        cfg = r.get("config") or {}
        url = cfg.get("stack_url", "") if isinstance(cfg, dict) else ""
        default = "yes" if r.get("is_default") else ""
        typer.echo(
            f"{r['id']:<26}  {r.get('name', ''):<{name_w}}  {r.get('source_type', ''):<{type_w}}  {default:<7}  {url}"
        )


@admin_connection_app.command("add")
def add_connection(
    name: str = typer.Option(..., "--name", help="Human-readable name"),
    stack_url: str = typer.Option(..., "--stack-url", help="Keboola stack URL"),
    token: str = typer.Option(..., "--token", help="Keboola Storage API token"),
    source_type: str = typer.Option("keboola", "--source-type", help="Source type"),
    default: bool = typer.Option(False, "--default/--no-default", help="Set as default connection"),
):
    """Add a named source connection and store its token in the vault."""
    payload = {
        "name": name,
        "source_type": source_type,
        "config": {"stack_url": stack_url},
        "is_default": default,
    }
    resp = api_post("/api/admin/source-connections", json=payload)
    if resp.status_code != 201:
        _fail(resp)
    body = resp.json()
    conn_id = body.get("id")
    typer.echo(f"Created connection id={conn_id}")

    secret_resp = api_put(
        f"/api/admin/source-connections/{conn_id}/secret",
        json={"value": token},
    )
    if secret_resp.status_code not in (200, 204):
        _fail(secret_resp)
    typer.echo(f"Token stored in vault for connection {conn_id}")


@admin_connection_app.command("remove")
def remove_connection(
    connection_id: str = typer.Argument(..., help="Connection id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Remove a named source connection."""
    if not yes:
        confirm = typer.confirm(f"Delete connection {connection_id}?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/admin/source-connections/{connection_id}")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Deleted connection {connection_id}")


@admin_connection_app.command("test")
def test_connection(
    connection_id: str = typer.Argument(..., help="Connection id"),
):
    """Test connectivity for a named source connection."""
    resp = api_post(f"/api/admin/source-connections/{connection_id}/test", json={})
    if resp.status_code not in (200, 201):
        _fail(resp)
    body = resp.json()
    if body.get("ok"):
        project = body.get("project_name", "")
        typer.echo(f"OK — project: {project}" if project else "OK")
    else:
        typer.echo(f"FAILED — {body.get('error', 'unknown error')}", err=True)
        raise typer.Exit(1)
