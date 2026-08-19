"""``agnes connectors`` — discover and read connector setup prompts.

The install prompt references connectors by name instead of inlining every
SKILL.md body: ``agnes connectors list`` shows what this instance offers
(seed-derived manifest, RBAC-free — the manifest is instance-wide), and
``agnes connectors show <slug>`` prints the full inline setup prompt for
one connector, ready to follow in a Claude Code session.

Server surface: ``GET /api/connectors/manifest`` and
``GET /api/connectors/{slug}/prompt`` (``app/api/connectors.py``).
"""

from __future__ import annotations

import json

import typer

from cli.v2_client import V2ClientError, api_get_json

connectors_app = typer.Typer(help="Discover and read setup prompts for this instance's optional tools.")


@connectors_app.command("list")
def list_connectors(
    json_out: bool = typer.Option(False, "--json", help="Raw manifest JSON."),
):
    """List the connectors this instance offers (Asana, Atlassian, …)."""
    try:
        body = api_get_json("/api/connectors/manifest")
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    if json_out:
        typer.echo(json.dumps(body, indent=2))
        return
    connectors = body.get("connectors", [])
    if not connectors:
        typer.echo("No connectors are configured on this instance.")
        return
    for c in connectors:
        marker = "required" if c.get("required") else "optional"
        typer.echo(
            f"  {c['slug']:24s} {c['display_name']:28s} "
            f"[{marker}, ~{c.get('estimated_minutes', '?')} min]  "
            f"{c.get('short_summary', '')}"
        )
    # Origin labeling (command-UX standard): say where the manifest came
    # from — the operator's Initial Workspace Template or the bundled seed.
    typer.echo(f"source: {body.get('source', 'unknown')}")
    typer.echo("Set one up with: agnes connectors show <slug>")


@connectors_app.command("show")
def show_connector(
    slug: str = typer.Argument(..., help="Connector slug, e.g. connector-asana"),
    json_out: bool = typer.Option(False, "--json", help="Raw response JSON."),
):
    """Print one connector's full setup prompt (follow it in a Claude Code session)."""
    try:
        body = api_get_json(f"/api/connectors/{slug}/prompt")
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    if json_out:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(body["prompt"])
