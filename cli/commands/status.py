"""Status commands — da status."""

import json

import typer

from cli.client import api_get
from cli.config import get_sync_state

status_app = typer.Typer(help="System status")


@status_app.callback(invoke_without_command=True)
def status(
    local: bool = typer.Option(False, "--local", help="Show local-only status (no server)"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show system health and sync status."""
    if local:
        state = get_sync_state()
        info = {
            "mode": "local",
            "tables_synced": len(state.get("tables", {})),
            "last_sync": state.get("last_sync", "never"),
            "tables": state.get("tables", {}),
        }
        if as_json:
            typer.echo(json.dumps(info, indent=2))
        else:
            typer.echo(f"Mode: offline (local data)")
            typer.echo(f"Tables synced: {info['tables_synced']}")
            typer.echo(f"Last sync: {info['last_sync']}")
        return

    try:
        # Minimal health ping first
        resp = api_get("/api/health")
        minimal = resp.json()
        if minimal.get("status") != "ok":
            if as_json:
                typer.echo(json.dumps(minimal, indent=2))
            else:
                typer.echo(f"Status: {minimal.get('status', 'unknown')}")
            return

        # Detailed health (auth required) for service-level info
        try:
            resp = api_get("/api/health/detailed")
            data = resp.json()
        except Exception:
            data = minimal

        if as_json:
            typer.echo(json.dumps(data, indent=2))
        else:
            typer.echo(f"Status: {data.get('status', 'unknown')}")
            for name, check in data.get("services", {}).items():
                s = check.get("status", "?")
                detail = ""
                if "tables" in check:
                    detail = f" ({check['tables']} tables, {check.get('total_rows', 0)} rows)"
                if "count" in check:
                    detail = f" ({check['count']})"
                if check.get("stale_tables"):
                    detail += f" [stale: {', '.join(check['stale_tables'])}]"
                typer.echo(f"  {name}: {s}{detail}")
    except Exception as e:
        typer.echo(f"Cannot reach server: {e}", err=True)
        typer.echo("Use --local for offline status.")
        raise typer.Exit(1)
