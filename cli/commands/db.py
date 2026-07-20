"""CLI: `agnes admin db {state,migrate,job,cancel}`.

Talks to the live server through the `/api/admin/db/*` endpoints
(PAT-authed via the shared `cli.client` helpers — same contract as
`agnes admin news`, `agnes admin add-user`, etc.). Direct-DB access would
race the running server's DuckDB write lock; HTTP is the right boundary.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""

from __future__ import annotations

import json as _json
import sys
import time

import typer

from cli.client import api_get, api_post

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


@db_app.command("migrate")
def migrate(
    target: str = typer.Argument(..., help="Target backend: side_car or cloud"),
    cloud_url: str = typer.Option(
        None,
        "--cloud-url",
        help="Cloud Postgres connection string (required when target=cloud)",
    ),
    detach: bool = typer.Option(
        False,
        "--detach",
        help="Return immediately with the job id; don't poll progress",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Output JSON (implies --detach behavior for stdout)",
    ),
    timeout: int = typer.Option(
        600,
        "--timeout",
        help="Max seconds to wait for completion when polling (default 600)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation. Required for non-interactive shells.",
    ),
) -> None:
    """Migrate to the next backend state (``side_car`` or ``cloud``).

    Spawns a server-side migration job via ``POST /api/admin/db/migrate``.
    Without ``--detach``, polls ``/api/admin/db/job/{id}`` every 2s and
    prints step transitions until the job reaches a terminal state.
    """
    if target not in ("side_car", "cloud"):
        typer.echo(
            f"invalid target {target!r}: expected 'side_car' or 'cloud'",
            err=True,
        )
        raise typer.Exit(2)

    if target == "cloud" and not cloud_url:
        cloud_url = typer.prompt("Cloud PG connection string")

    # MED-1: ``--json`` does NOT bypass the confirmation gate. CI/cron
    # callers must opt in explicitly with ``--yes``. The earlier
    # ``and not as_json`` clause meant a ``--json`` invocation skipped
    # the destructive-cutover confirm and auto-fired the migration.
    needs_confirm = not yes
    if needs_confirm:
        if not sys.stdin.isatty():
            typer.echo(
                "Refusing destructive migrate without --yes in non-interactive shell. "
                "Re-run with --yes (or -y) to proceed.",
                err=True,
            )
            raise typer.Exit(2)
        if not typer.confirm(
            f"Migrate app-state DB to '{target}'? This is operator-level + destructive on failure.",
            default=False,
        ):
            typer.echo("Cancelled by operator.", err=True)
            raise typer.Exit(1)

    payload: dict = {"target": target}
    if cloud_url:
        payload["cloud_url"] = cloud_url

    resp = api_post("/api/admin/db/migrate", json=payload)
    body = _exit_on_error(resp, expected_status=(200, 202))
    job_id = body.get("job_id")
    if not job_id:
        typer.echo(f"server did not return a job_id: {body!r}", err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(_json.dumps(body))
        return

    if detach:
        typer.echo(f"Job started: {job_id}")
        return

    # Poll for progress.
    typer.echo(f"Job started: {job_id} (polling, Ctrl-C to detach)")
    deadline = time.time() + timeout
    last_step = None
    while time.time() < deadline:
        time.sleep(2)
        jr = api_get(f"/api/admin/db/job/{job_id}")
        if jr.status_code != 200:
            typer.echo(f"poll error {jr.status_code}", err=True)
            continue
        job = jr.json()
        step = job.get("current_step")
        if step != last_step:
            pct = job.get("progress_pct", 0)
            typer.echo(f"  [{pct:>3}%] {step}")
            last_step = step
        status = job.get("status")
        if status in ("success", "failed", "cancelled"):
            typer.echo(f"  Result: {status}")
            if status == "failed":
                err = job.get("error") or {}
                typer.echo(
                    f"  Error at {err.get('step')}: {err.get('message')}",
                    err=True,
                )
                raise typer.Exit(1)
            return
    typer.echo(
        f"timeout — job still running. Run `agnes admin db job {job_id}` to check.",
        err=True,
    )
    raise typer.Exit(2)


@db_app.command("job")
def job(
    job_id: str = typer.Argument(..., help="Migration job id (from `db migrate`)"),
    as_json: bool = typer.Option(False, "--json", help="Output JSON for scripting"),
) -> None:
    """Show the status of a migration job."""
    resp = api_get(f"/api/admin/db/job/{job_id}")
    data = _exit_on_error(resp)

    if as_json:
        typer.echo(_json.dumps(data, indent=2))
        return

    typer.echo(f"Job:    {data.get('job_id')}")
    typer.echo(f"Status: {data.get('status')}")
    step = data.get("current_step")
    pct = data.get("progress_pct", 0)
    typer.echo(f"Step:   {step} ({pct}%)")
    err = data.get("error")
    if err:
        typer.echo(f"Error:  {err.get('message')} (at {err.get('step')})")
    summary = data.get("summary")
    if summary:
        typer.echo(f"Summary: {summary}")


@db_app.command("cancel")
def cancel(
    job_id: str = typer.Argument(..., help="Migration job id to cancel"),
) -> None:
    """Cancel a running migration job (rejected past point-of-no-return)."""
    resp = api_post(f"/api/admin/db/cancel/{job_id}")
    _exit_on_error(resp)
    typer.echo(f"Job {job_id} cancelled.")


@db_app.command("repair")
def repair(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Rebuild a corrupt ``system.duckdb`` in place (EXPORT/IMPORT).

    Heals on-disk ART (PRIMARY KEY / UNIQUE) index corruption — the
    "Failed to delete all rows from index" / "database has been invalidated"
    crash a plain restart cannot fix. Data is preserved; the corrupt
    original is kept as ``system.duckdb.broken.<ts>``.

    Runs directly against the state file (NOT the HTTP API): the API is
    unusable when the DB is invalidated, which is exactly when you need
    this. DuckDB allows a single writer, so **stop the app first** or the
    rebuild fails on the file lock. Note the server also self-heals this on
    start — restarting it is usually enough; this command forces a rebuild
    without a full restart cycle.
    """
    import duckdb

    import src.db as _db
    from src.repositories import use_pg

    if use_pg():
        typer.echo("App-state backend is Postgres; system.duckdb repair does not apply.")
        return

    db_path = _db._get_state_dir() / "system.duckdb"
    if not db_path.exists():
        typer.echo(f"No system.duckdb at {db_path}; nothing to repair.", err=True)
        raise typer.Exit(1)

    if not yes:
        if not sys.stdin.isatty():
            typer.echo(
                "Refusing to repair without --yes in a non-interactive shell. Stop the app, then re-run with --yes.",
                err=True,
            )
            raise typer.Exit(1)
        if not typer.confirm(
            f"Rebuild {db_path} via EXPORT/IMPORT? Stop the app first. "
            "The corrupt original is preserved as .broken.<ts>."
        ):
            raise typer.Abort()

    try:
        broken = _db._rebuild_system_db(str(db_path))
    except duckdb.Error as e:
        typer.echo(
            f"Repair failed: {e}\nIs the app still running? DuckDB is single-writer — stop the app and retry.",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(f"Rebuilt {db_path}. Corrupt original preserved at {broken}.")
