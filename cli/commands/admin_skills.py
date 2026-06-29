"""`agnes admin skill` — contributed-skill management CLI.

agnes admin skill list              — list contributed plugins
agnes admin skill contribute <file> — publish a skill from file or stdin
agnes admin skill delete <name>     — remove a contributed skill
"""

from __future__ import annotations

import json
import sys

import typer
from rich.console import Console
from rich.table import Table

from cli.client import api_delete, api_get, api_post

admin_skills_app = typer.Typer(help="Contributed skills management")

_console = Console()


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


@admin_skills_app.command("list")
def skill_list(
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """List contributed plugins in the Agnes Contributed marketplace."""
    resp = api_get("/api/admin/contributed-skills")
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    plugins = data.get("plugins", [])
    if as_json:
        typer.echo(json.dumps(plugins, indent=2))
        return
    table = Table(title=f"Contributed skills ({len(plugins)})")
    table.add_column("NAME", style="cyan", no_wrap=True)
    table.add_column("VERSION")
    table.add_column("DESCRIPTION")
    table.add_column("GRANT GROUP")
    for p in plugins:
        table.add_row(
            str(p.get("name") or ""),
            str(p.get("version") or ""),
            str(p.get("description") or ""),
            str(p.get("grant_group") or ""),
        )
    _console.print(table)


@admin_skills_app.command("contribute")
def skill_contribute(
    file: str = typer.Argument(..., help="Path to SKILL.md, or '-' for stdin"),
    group: str = typer.Option("Admin", "--group", "-g", help="Grant group (default: Admin)"),
) -> None:
    """Publish a SKILL.md into the Agnes Contributed marketplace."""
    if file == "-":
        skill_md = sys.stdin.read()
    else:
        try:
            with open(file, encoding="utf-8") as f:
                skill_md = f.read()
        except OSError as e:
            typer.echo(f"Cannot read file: {e}", err=True)
            raise typer.Exit(1)

    resp = api_post(
        "/api/admin/contributed-skills",
        json={"skill_md": skill_md, "grant_group": group},
    )
    if resp.status_code not in (200, 201):
        _fail(resp)
    result = resp.json()
    typer.echo(
        f"Published '{result.get('skill_name')}' as plugin '{result.get('plugin_name')}' "
        f"(granted to: {result.get('granted_group') or 'none'})"
    )


@admin_skills_app.command("delete")
def skill_delete(
    name: str = typer.Argument(..., help="Plugin name to remove"),
) -> None:
    """Remove a contributed skill plugin by name."""
    resp = api_delete(f"/api/admin/contributed-skills/{name}")
    if resp.status_code == 204:
        typer.echo(f"Deleted plugin '{name}'.")
    else:
        _fail(resp)
