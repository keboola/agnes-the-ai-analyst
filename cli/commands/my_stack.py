"""`agnes my-stack show` — read-only view of the user's current marketplace stack.

Reads ``GET /api/my-stack``. To add or remove items use
``agnes marketplace add/remove``.
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from cli.v2_client import V2ClientError, api_get_json

my_stack_app = typer.Typer(help="Show your current marketplace stack (use 'agnes marketplace' to add/remove)")


@my_stack_app.command("show")
def show_stack(
    json_out: bool = typer.Option(False, "--json"),
):
    """Show curated plugins available to subscribe to and your Flea Market installs."""
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
    typer.echo(f"Curated plugins: {len(curated)}")
    for p in curated:
        flag = "✓" if p["enabled"] else "✗"
        typer.echo(
            f"  [{flag}] {p['marketplace_id']}/{p['plugin_name']:24s} "
            f"manifest={p['manifest_name']} v{p.get('version') or '?'}"
        )
    typer.echo(f"\nFrom Flea Market: {len(store)}")
    for it in store:
        typer.echo(
            f"  [{it['type']:6s}] {it['name']:24s} by {it['owner_username']:20s} "
            f"invocation={it['invocation_name']}  id={it['entity_id']}"
        )


