"""`agnes admin jobs` — CLI over the wave-2B job queue (spec §3.3, Task 5).

CLI counterpart to the ``/api/jobs`` surface. Each subcommand maps 1:1 to
one HTTP endpoint:

  - ``enqueue`` → ``POST /api/jobs``
  - ``show``    → ``GET  /api/jobs/{job_id}``
  - ``list``    → ``GET  /api/jobs``

``enqueue --payload`` takes inline JSON (or ``@path/to.json`` to read from
disk), following the same ``@file`` convention as ``agnes admin
register-table --query``/``--where-filters-json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get, api_post

admin_jobs_app = typer.Typer(help="Job queue admin (wave-2B worker runtime)")


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


def _print_job(job: dict) -> None:
    typer.echo(f"  id:              {job.get('id')}")
    typer.echo(f"  kind:            {job.get('kind')}")
    typer.echo(f"  status:          {job.get('status')}")
    typer.echo(f"  priority:        {job.get('priority')}")
    typer.echo(f"  attempts:        {job.get('attempts')}/{job.get('max_attempts')}")
    if job.get("idempotency_key"):
        typer.echo(f"  idempotency_key: {job.get('idempotency_key')}")
    if job.get("error"):
        typer.echo(f"  error:           {job.get('error')}")
    typer.echo(f"  created_at:      {job.get('created_at')}")
    if job.get("started_at"):
        typer.echo(f"  started_at:      {job.get('started_at')}")
    if job.get("finished_at"):
        typer.echo(f"  finished_at:     {job.get('finished_at')}")
    typer.echo(f"  payload:         {json.dumps(job.get('payload') or {})}")


@admin_jobs_app.command("enqueue")
def enqueue(
    kind: str = typer.Argument(..., help="Registered job kind (see server error for the live list if unknown)"),
    payload: str = typer.Option(
        "",
        "--payload",
        help="JSON payload object. Inline JSON or @path/to.json.",
    ),
    idempotency_key: Optional[str] = typer.Option(
        None,
        "--idempotency-key",
        help="Dedup key: a matching queued/running job is returned unchanged instead of duplicated.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Enqueue a job. Fails with a 400 listing registered kinds if `kind` is unknown."""
    payload_dict: dict = {}
    if payload:
        text = payload
        if payload.startswith("@"):
            p = Path(payload[1:])
            if not p.exists():
                typer.echo(f"Error: payload file not found: {p}", err=True)
                raise typer.Exit(2)
            text = p.read_text(encoding="utf-8")
        try:
            payload_dict = json.loads(text)
        except json.JSONDecodeError as e:
            typer.echo(f"Error: --payload is not valid JSON: {e}", err=True)
            raise typer.Exit(2)

    body: dict = {"kind": kind, "payload": payload_dict}
    if idempotency_key:
        body["idempotency_key"] = idempotency_key

    resp = api_post("/api/jobs", json=body)
    if resp.status_code != 202:
        _fail(resp)
    job = resp.json()["job"]
    if as_json:
        typer.echo(json.dumps(job, indent=2))
        return
    typer.echo(f"Enqueued job {job['id']} (kind={job['kind']}, status={job['status']})")


@admin_jobs_app.command("show")
def show(
    job_id: str = typer.Argument(..., help="Job id"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show one job's full detail."""
    resp = api_get(f"/api/jobs/{job_id}")
    if resp.status_code == 404:
        typer.echo(f"Job not found: {job_id}", err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        _fail(resp)
    job = resp.json()["job"]
    if as_json:
        typer.echo(json.dumps(job, indent=2))
        return
    _print_job(job)


@admin_jobs_app.command("list")
def list_jobs(
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status (queued|running|done|failed)"),
    kind: Optional[str] = typer.Option(None, "--kind", help="Filter by job kind"),
    limit: int = typer.Option(50, "--limit", help="Max rows to return"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List jobs, most recent first."""
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    if kind:
        params["kind"] = kind
    resp = api_get("/api/jobs", params=params)
    if resp.status_code != 200:
        _fail(resp)
    jobs = resp.json()["jobs"]
    if as_json:
        typer.echo(json.dumps(jobs, indent=2))
        return
    typer.echo(f"Jobs: {len(jobs)}")
    if not jobs:
        return
    typer.echo(f"{'ID':<34}  {'KIND':<20}  {'STATUS':<10}  {'ATTEMPTS':<10}  CREATED")
    for j in jobs:
        typer.echo(
            f"{j['id']:<34}  {j['kind']:<20}  {j['status']:<10}  "
            f"{j['attempts']}/{j['max_attempts']:<8}  {j.get('created_at', '')}"
        )
