"""CLI: `agnes admin db state` (and later: migrate, job, cancel).

Talks to the live server through the `/api/admin/db/*` endpoints
(PAT-authed via the shared `cli.client` helpers — same contract as
`agnes admin news`, `agnes admin add-user`, etc.). Direct-DB access would
race the running server's DuckDB write lock; HTTP is the right boundary.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations

import json as _json

import typer

from cli.client import api_get

db_app = typer.Typer(help="Manage Agnes app-state DB backend (DuckDB / Postgres)")


def _exit_on_error(resp, expected_status: tuple[int, ...] = (200,)) -> dict:
    """Print server-side error detail and exit if status is unexpected."""
    if resp.status_code in expected_status:
        try:
            return resp.json()
        except Exception:
            return {}
    detail = ""
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else body
    except Exception:
        detail = resp.text
    typer.echo(f"server returned {resp.status_code}: {detail}", err=True)
    raise typer.Exit(1)


@db_app.command("state")
def state(
    as_json: bool = typer.Option(False, "--json", help="Output JSON for scripting"),
) -> None:
    """Show current DB backend state, allowed transitions, and any active job."""
    resp = api_get("/api/admin/db/state")
    data = _exit_on_error(resp)

    if as_json:
        typer.echo(_json.dumps(data, indent=2))
        return

    typer.echo(f"Backend:     {data.get('backend')}")
    typer.echo(f"URL:         {data.get('url_redacted') or '(none)'}")
    transitions = data.get("allowed_transitions") or []
    typer.echo(f"Transitions: {', '.join(transitions) if transitions else '(terminal)'}")
    if data.get("current_job_id"):
        typer.echo(f"Active job:  {data['current_job_id']}")
