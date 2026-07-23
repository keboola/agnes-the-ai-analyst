"""CLI: `agnes admin analytics migrate --to ducklake|legacy` (wave-2G Task 6).

Talks to the live server through `POST /api/admin/analytics/migrate`
(PAT-authed via the shared `cli.client` helpers — same contract as
`agnes admin db migrate` / `agnes admin jobs enqueue`).
"""

from __future__ import annotations

import json as _json

import typer

from cli.client import api_post

analytics_app = typer.Typer(help="DuckLake analytics-backend migration (wave-2G)")


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and detail.get("problems"):
        typer.echo(f"Error ({resp.status_code}): {detail.get('error', 'request failed')}", err=True)
        for problem in detail["problems"]:
            typer.echo(f"  - {problem}", err=True)
    elif isinstance(detail, dict):
        typer.echo(f"Error ({resp.status_code}): {_json.dumps(detail)}", err=True)
    else:
        msg = detail if isinstance(detail, str) else (resp.text or f"HTTP {resp.status_code}")
        typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


@analytics_app.command("migrate")
def migrate(
    to: str = typer.Option(..., "--to", help="Target analytics backend: 'ducklake' or 'legacy'"),
    as_json: bool = typer.Option(False, "--json", help="Output JSON for scripting"),
) -> None:
    """Validate prerequisites, then enqueue a full rebuild into the target
    analytics backend from the on-disk extracts tree.

    This command never flips `analytics.backend` in config — it validates
    (only for `--to ducklake`: the DuckLake extension is loadable and the
    catalog is reachable, auto-repairing a missing catalog database on an
    existing Postgres volume where the init-script never ran) and then
    enqueues the rebuild job. Once the job (`agnes admin jobs show
    <job_id>`) finishes, set `analytics.backend` in `instance.yaml` (or
    `AGNES_ANALYTICS_BACKEND` env) on every role process and restart —
    config is read once at boot, not hot-reloaded.
    """
    resp = api_post("/api/admin/analytics/migrate", json={"to": to})
    if resp.status_code not in (200, 202, 409):
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(_json.dumps(body))
        if resp.status_code == 409:
            raise typer.Exit(1)
        return

    if resp.status_code == 409:
        detail = body.get("detail") if isinstance(body, dict) else body
        job_id = detail.get("job_id") if isinstance(detail, dict) else None
        typer.echo(
            f"A migration is already in progress (job {job_id}). Check `agnes admin jobs show {job_id}` for status.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Status:  {body.get('status')}")
    typer.echo(f"Target:  {body.get('to')}")
    typer.echo(f"Job:     {body.get('job_id')}")
    typer.echo("")
    typer.echo(body.get("message", ""))
