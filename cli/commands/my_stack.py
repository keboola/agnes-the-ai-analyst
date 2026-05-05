"""`agnes my-stack {show,toggle}` — per-user marketplace composition view.

Reads ``GET /api/my-stack`` and writes
``PUT /api/my-stack/curated/{marketplace_id}/{plugin_name}`` opt-out flips.
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from cli.v2_client import V2ClientError, api_get_json, api_put_json

my_stack_app = typer.Typer(help="Per-user marketplace composition (curated grants + Store installs)")


@my_stack_app.command("show")
def show_stack(
    json_out: bool = typer.Option(False, "--json"),
):
    """Show admin-granted plugins (with opt-out state) and your Store installs."""
    try:
        body = api_get_json("/api/my-stack")
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    if json_out:
        typer.echo(json.dumps(body, indent=2))
        return
    curated = body.get("curated", [])
    store = body.get("store", [])
    typer.echo(f"Curated (admin-granted): {len(curated)}")
    for p in curated:
        flag = "✓" if p["enabled"] else "✗"
        typer.echo(
            f"  [{flag}] {p['marketplace_id']}/{p['plugin_name']:24s} "
            f"manifest={p['manifest_name']} v{p.get('version') or '?'}"
        )
    typer.echo(f"\nFrom Store: {len(store)}")
    for it in store:
        typer.echo(
            f"  [{it['type']:6s}] {it['name']:24s} by {it['owner_username']:20s} "
            f"invocation={it['invocation_name']}  id={it['entity_id']}"
        )


@my_stack_app.command("toggle")
def toggle(
    marketplace_id: str = typer.Argument(...),
    plugin_name: str = typer.Argument(...),
    on: bool = typer.Option(False, "--on", help="Enable (drop opt-out)"),
    off: bool = typer.Option(False, "--off", help="Disable (set opt-out)"),
):
    """Toggle a curated plugin on or off (writes a `user_plugin_optouts` row)."""
    if on == off:
        typer.echo("Pass exactly one of --on / --off.", err=True)
        raise typer.Exit(2)
    enabled = bool(on)
    path = f"/api/my-stack/curated/{marketplace_id}/{plugin_name}"
    try:
        api_put_json(path, {"enabled": enabled})
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    state = "ENABLED" if enabled else "DISABLED"
    typer.echo(f"{marketplace_id}/{plugin_name}: {state}")
