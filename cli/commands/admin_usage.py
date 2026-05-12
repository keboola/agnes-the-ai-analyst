"""`agnes admin usage` — telemetry export from the terminal."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from cli.client import get_client

app = typer.Typer(help="Telemetry export and admin queries.")


def _handle_error(resp, context: str) -> None:
    """Print a clean error and exit non-zero for non-2xx responses."""
    if resp.status_code in (401, 403):
        typer.echo(
            "[err] authentication required — run `agnes auth login` or import a PAT",
            err=True,
        )
        raise typer.Exit(1)
    if resp.status_code >= 400:
        try:
            body = resp.json().get("detail", resp.text)
        except Exception:
            body = resp.text
        typer.echo(f"[err] server returned {resp.status_code}: {body}", err=True)
        raise typer.Exit(1)


@app.command()
def export(
    format: str = typer.Option("csv", "--format", help="csv|json|parquet"),
    since: Optional[str] = typer.Option(None, "--since"),
    until: Optional[str] = typer.Option(None, "--until"),
    user: Optional[str] = typer.Option(None, "--user"),
    source: Optional[str] = typer.Option(None, "--source"),
    out: Optional[Path] = typer.Option(None, "--out", help="Write to file; else stdout."),
):
    """Export telemetry events filtered by since/until/user/source."""
    if format not in ("csv", "json", "parquet"):
        typer.echo(f"[err] format must be csv|json|parquet, got {format!r}", err=True)
        raise typer.Exit(1)

    params: dict = {"format": format}
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if user:
        params["user_id"] = user
    if source:
        params["source"] = source

    try:
        with get_client(timeout=120.0) as client:
            with client.stream("GET", "/api/admin/usage/export", params=params) as resp:
                if resp.status_code in (401, 403):
                    typer.echo(
                        "[err] authentication required — run `agnes auth login` or import a PAT",
                        err=True,
                    )
                    raise typer.Exit(1)
                if resp.status_code >= 400:
                    body = resp.read().decode(errors="replace")
                    typer.echo(f"[err] server returned {resp.status_code}: {body}", err=True)
                    raise typer.Exit(1)

                sink = out.open("wb") if out else sys.stdout.buffer
                try:
                    for chunk in resp.iter_bytes():
                        sink.write(chunk)
                    if out:
                        typer.echo(f"wrote {out}", err=True)
                finally:
                    if out:
                        sink.close()
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"[err] cannot reach server: {exc}", err=True)
        raise typer.Exit(1)


@app.command()
def reprocess():
    """Force re-extraction of all sessions for the usage processor."""
    client = get_client(timeout=60)
    try:
        resp = client.post("/api/admin/usage/reprocess")
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)
    if resp.status_code == 401:
        typer.echo("[err] authentication required", err=True)
        raise typer.Exit(1)
    if resp.status_code == 403:
        typer.echo("[err] admin only", err=True)
        raise typer.Exit(1)
    if resp.status_code >= 400:
        typer.echo(f"[err] {resp.status_code}: {resp.text}", err=True)
        raise typer.Exit(1)
    data = resp.json()
    typer.echo("Reprocess scheduled — UsageProcessor will re-extract on next scheduler tick.")
    for k, v in data.get("deleted", {}).items():
        typer.echo(f"  deleted {k}: {v}")


@app.command()
def prune(
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of summary."),
):
    """Prune usage_events older than USAGE_EVENTS_RETENTION_DAYS env var on the server."""
    client = get_client(timeout=60)
    try:
        resp = client.post("/api/admin/usage/prune")
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)
    if resp.status_code == 401:
        typer.echo("[err] authentication required", err=True)
        raise typer.Exit(1)
    if resp.status_code == 403:
        typer.echo("[err] admin only", err=True)
        raise typer.Exit(1)
    if resp.status_code >= 400:
        typer.echo(f"[err] {resp.status_code}: {resp.text}", err=True)
        raise typer.Exit(1)
    data = resp.json()
    if json_out:
        import json
        typer.echo(json.dumps(data, indent=2))
        return
    if data.get("status") == "skipped":
        typer.echo(f"Skipped: {data.get('reason')}")
    else:
        typer.echo(
            f"Pruned {data['deleted']} events older than {data['retention_days']} days; "
            f"{data['remaining']} remain."
        )
