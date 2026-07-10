"""`agnes chat` — CLI surface for the web chat REST API.

Currently one subcommand:

  - `agnes chat skills [--json]` — mirrors `GET /api/chat/skills`, the
    server-normalized skills + commands catalog that backs the web chat
    composer's slash menu (see `app/chat/skills_catalog.py`).
"""

from __future__ import annotations

import json

import typer

from cli.client import api_get

chat_app = typer.Typer(help="Cloud chat — skills/commands catalog")


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    msg = detail if isinstance(detail, str) and detail else (resp.text or f"HTTP {resp.status_code}")
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


@chat_app.command("skills")
def chat_skills(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """List skills + slash commands invokable in your web chat sandbox.

    Merges the bundled chat workspace-template skills with your
    RBAC-filtered marketplace/store plugin skills (marketplace wins name
    clashes) — the same set installed into your live chat sandbox.
    """
    resp = api_get("/api/chat/skills")
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}

    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return

    skills: list[dict] = body.get("skills", [])
    commands: list[dict] = body.get("commands", [])

    if not skills and not commands:
        typer.echo("No skills or commands available.")
        return

    if skills:
        typer.echo("Skills:")
        name_w = max(len("NAME"), max((len(s.get("name", "")) for s in skills), default=4))
        for s in skills:
            desc = s.get("description") or ""
            typer.echo(f"  {s.get('name', ''):<{name_w}}  [{s.get('source', '')}]  {desc}")

    if commands:
        if skills:
            typer.echo("")
        typer.echo("Commands:")
        for c in commands:
            desc = c.get("description") or ""
            typer.echo(f"  {c.get('name', ''):<20}  {desc}")
