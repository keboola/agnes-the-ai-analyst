"""`agnes chat` — CLI surface for the web chat REST API.

Subcommands:

  - `agnes chat skills [--json]` — mirrors `GET /api/chat/skills`, the
    server-normalized skills + commands catalog that backs the web chat
    composer's slash menu (see `app/chat/skills_catalog.py`).
  - `agnes chat upload <file> [--kind data|image|document]
      [--as-table NAME] [--json]` — upload a local file into your chat
    workspace via `POST /api/chat/uploads`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get, api_post

chat_app = typer.Typer(help="Cloud chat — skills/commands catalog and file uploads")


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


@chat_app.command("upload")
def chat_upload(
    file: Path = typer.Argument(..., help="Local file to upload into your chat workspace."),
    kind: str = typer.Option(
        "data",
        "--kind",
        help="File kind: data | image | document",
    ),
    as_table: Optional[str] = typer.Option(
        None,
        "--as-table",
        help="Register uploaded data file as a workspace-local queryable table with this name.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON response."),
) -> None:
    """Upload a local file into your chat workspace (POST /api/chat/uploads).

    The file lands in your per-user workspace ``uploads/`` folder and is
    available to Claude in your next chat sandbox session.

    For data files (CSV, parquet, XLSX) pass ``--as-table NAME`` to register
    the file as a workspace-local queryable table so ``agnes query`` can reach
    it in-session without an admin table-registry entry.

    Examples::

        agnes chat upload data.csv --kind data --as-table my_data
        agnes chat upload report.pdf --kind document
        agnes chat upload chart.png --kind image --json
    """
    if not file.exists():
        typer.echo(
            f"Error: file '{file}' not found. Check the path and try again.",
            err=True,
        )
        raise typer.Exit(1)

    valid_kinds = {"data", "image", "document"}
    if kind not in valid_kinds:
        typer.echo(
            f"Error: unknown kind '{kind}'. Choose one of: {', '.join(sorted(valid_kinds))}",
            err=True,
        )
        raise typer.Exit(1)

    data: dict[str, str] = {"kind": kind}
    if as_table is not None:
        data["register_as_table"] = "true"
        data["table_name"] = as_table

    with file.open("rb") as fh:
        resp = api_post(
            "/api/chat/uploads",
            files={"file": (file.name, fh, _guess_content_type(file))},
            data=data,
        )

    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return

    typer.echo(f"Uploaded: {body.get('filename')}  ({body.get('size_bytes', 0):,} bytes)")
    if body.get("table_name"):
        typer.echo(f"  Registered as table: {body['table_name']}")
    typer.echo(f"  Workspace path: {body.get('workspace_path')}")
    hint = body.get("hint")
    if hint:
        typer.echo(f"  Hint: {hint}")


def _guess_content_type(path: Path) -> str:
    """Return a plausible MIME type for a file based on its extension."""
    _MAP = {
        ".csv": "text/csv",
        ".parquet": "application/octet-stream",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
    }
    return _MAP.get(path.suffix.lower(), "application/octet-stream")
