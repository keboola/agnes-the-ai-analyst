"""`agnes app ...` — manage hosted data apps.

Consumes the control-plane REST surface documented in
``app/api/data_apps.py`` (Task 7 of the data-apps platform plan):

  - ``list``           GET    /api/data-apps
  - ``show <slug>``    GET    /api/data-apps/{slug}
  - ``create``         POST   /api/data-apps
  - ``deploy <slug>``  POST   /api/data-apps/{slug}/deploy
  - ``logs <slug>``    GET    /api/data-apps/{slug}/logs
  - ``open <slug>``    GET    /api/data-apps/{slug}          (prints url only)
  - ``stop <slug>``    POST   /api/data-apps/{slug}/stop
  - ``delete <slug>``  DELETE /api/data-apps/{slug}

``open`` is deliberately print-only — no browser launch — so headless
environments (CI, remote shells) behave identically to a desktop one.

Secrets management (``PUT /api/data-apps/{slug}/secrets``) and the
admin/scheduler-only ``POST /api/data-apps/reap-idle`` have no CLI command;
see the exemption reasons in
``tests/test_documentation_api_triple_surface.py``.
"""

from __future__ import annotations

import json as json_lib
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_post

data_apps_app = typer.Typer(help="Manage hosted data apps")

# Maps the REST `detail` error codes (see app/api/data_apps.py's HTTPException
# call sites) to a human-actionable message. Unknown/unmapped details fall
# back to the raw string so a new server-side error code is never swallowed.
_ERROR_MESSAGES = {
    "app_quota_exceeded": "You've hit your data-app quota for this account. Stop or delete one before creating another.",
    "slug_exists": "That slug is already taken. Pick a different one.",
    "invalid_slug": "Invalid slug — use lowercase letters, numbers, and hyphens only.",
    "invalid_repo_mode": "Invalid --repo-url/--repo-branch combination.",
    "create_in_progress": "Another create request for your account is already in flight. Try again in a moment.",
    "deploy_empty_repo": "This app's repo has no commits yet — push something before deploying.",
    "runner_unavailable": "The data-app runner is unavailable right now. Try again shortly, or check `agnes status`.",
    "data_apps_disabled": "Data apps are not enabled on this server. Ask an admin to enable them in instance.yaml.",
    "forbidden": "You don't have access to this data app.",
    "data_app_not_found": "Data app not found.",
    "owner_not_found": "The app's owner account no longer exists on the server.",
}


def _detail(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return resp.text
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    return _ERROR_MESSAGES.get(detail, detail or resp.text)


def _fail(resp) -> None:
    typer.echo(f"Failed: {_detail(resp)}", err=True)
    raise typer.Exit(1)


def _not_found(slug: str) -> None:
    typer.echo(f"Data app not found: {slug}", err=True)
    typer.echo("Try: agnes app list  — to see the apps you can access.", err=True)
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@data_apps_app.command("list")
def list_apps(
    limit: int = typer.Option(20, "--limit", help="Max results"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """List data apps you can see (owner, Admin, or a granted group)."""
    resp = api_get("/api/data-apps")
    if resp.status_code != 200:
        _fail(resp)

    apps = resp.json()[:limit]

    if json:
        typer.echo(json_lib.dumps(apps, indent=2, default=str))
        return

    if not apps:
        typer.echo("No data apps found.")
        typer.echo("Try: agnes app create <slug> <name>  — to create one.")
        return

    typer.echo(f"{'SLUG':20s} {'NAME':20s} {'STATE':10s} URL")
    for a in apps:
        typer.echo(f"{a.get('slug', ''):20s} {a.get('name', ''):20s} {a.get('state', ''):10s} {a.get('url', '')}")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@data_apps_app.command("show")
def show_app(
    slug: str = typer.Argument(..., help="App slug"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show detail for one data app."""
    resp = api_get(f"/api/data-apps/{slug}")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    a = resp.json()
    if json:
        typer.echo(json_lib.dumps(a, indent=2, default=str))
        return

    typer.echo(f"Slug:        {a.get('slug', slug)}")
    typer.echo(f"Name:        {a.get('name', '')}")
    typer.echo(f"State:       {a.get('state', '')}")
    typer.echo(f"URL:         {a.get('url', '')}")
    if a.get("description"):
        typer.echo(f"Description: {a['description']}")
    if a.get("deployed_sha"):
        typer.echo(f"Deployed:    {a['deployed_sha']}")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@data_apps_app.command("create")
def create_app(
    slug: str = typer.Argument(..., help="URL-safe slug"),
    name: str = typer.Argument(..., help="Display name"),
    description: str = typer.Option("", "--description", help="Description"),
    repo_url: Optional[str] = typer.Option(None, "--repo-url", help="External git repo URL — sets repo_mode=external"),
    repo_branch: str = typer.Option("main", "--repo-branch", help="Branch to track (external repo mode only)"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Create a new data app.

    Defaults to an internal, server-hosted git repo (``repo_mode=internal``,
    the server default). Pass ``--repo-url`` to track an external repo
    instead (``repo_mode=external``); ``--repo-branch`` selects which branch
    of that repo is tracked (default ``main``).
    """
    payload: dict = {"slug": slug, "name": name, "description": description}
    if repo_url:
        payload["repo_mode"] = "external"
        payload["repo_url"] = repo_url
        payload["repo_branch"] = repo_branch

    resp = api_post("/api/data-apps", json=payload)
    if resp.status_code != 201:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(f"Created: slug={body.get('slug', slug)}")
    typer.echo(f"Git URL: {body.get('git_url', '')}")


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


@data_apps_app.command("deploy")
def deploy_app(
    slug: str = typer.Argument(..., help="App slug"),
    sha: Optional[str] = typer.Option(
        None, "--sha", help="Deploy this commit sha (default: fast-forward to the tracked branch's latest)"
    ),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Deploy (or redeploy) an app — fast-forwards ``agnes-live`` and hands off to the runner."""
    payload: dict = {}
    if sha:
        payload["sha"] = sha

    resp = api_post(f"/api/data-apps/{slug}/deploy", json=payload)
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(f"State: {body.get('state', '')}  deployed_sha={body.get('deployed_sha', '')}")


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


@data_apps_app.command("logs")
def logs_app(
    slug: str = typer.Argument(..., help="App slug"),
    tail: int = typer.Option(200, "--tail", help="Number of trailing log lines"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show the last N lines of runner logs for an app (owner/Admin only)."""
    resp = api_get(f"/api/data-apps/{slug}/logs", params={"tail": tail})
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(body.get("logs", ""))


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


@data_apps_app.command("open")
def open_app(slug: str = typer.Argument(..., help="App slug")):
    """Print the app's URL. Does NOT launch a browser — headless parity."""
    resp = api_get(f"/api/data-apps/{slug}")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    typer.echo(resp.json().get("url", ""))


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@data_apps_app.command("stop")
def stop_app(
    slug: str = typer.Argument(..., help="App slug"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Stop a running app."""
    resp = api_post(f"/api/data-apps/{slug}/stop")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return

    typer.echo(f"State: {body.get('state', '')}")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@data_apps_app.command("delete")
def delete_app(
    slug: str = typer.Argument(..., help="App slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete a data app (runner stop + service-token revoke + registry row delete)."""
    if not yes:
        confirmed = typer.confirm(f"Delete data app {slug}?")
        if not confirmed:
            raise typer.Abort()

    resp = api_delete(f"/api/data-apps/{slug}")
    if resp.status_code == 404:
        _not_found(slug)
    if resp.status_code != 204:
        _fail(resp)

    typer.echo(f"Deleted: {slug}")
