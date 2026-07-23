"""`agnes agent` — analyst-side CLI over the agent-profile API (Task 11,
`docs/superpowers/plans/2026-07-22-agent-api-v1a.md`).

Thin wrapper around the management surface (`app/api/agents_admin.py`,
`/api/v1/agents` CRUD + scope + PAT issuance — session-token only, every
route rejects a PAT) and the runtime surface (`app/api/agent_runtime.py`,
`POST /api/v1/agents/{slug}/responses` + `GET /api/v1/jobs/{id}` — callable
with either a session token or an agent PAT scoped to that agent).

Each subcommand maps 1:1 to one HTTP endpoint:

  - ``list``      -> ``GET  /api/v1/agents``
  - ``create``     -> ``POST /api/v1/agents``
  - ``show``       -> ``GET  /api/v1/agents`` (list + filter by slug — the
                       management API only supports lookup by id, and an
                       extra id-lookup round trip buys nothing the list
                       response doesn't already carry)
  - ``scope set``  -> ``PUT  /api/v1/agents/{id}/scope``
  - ``token``      -> ``POST /api/v1/agents/{id}/tokens``
  - ``delete``     -> ``DELETE /api/v1/agents/{id}``
  - ``ask``        -> ``POST /api/v1/agents/{slug}/responses`` (runtime
                       surface takes the slug directly, not the id), then
                       polls ``GET /api/v1/jobs/{id}`` on a `202` until the
                       job reaches a terminal status or ``--timeout`` runs out.

Every management subcommand except ``ask`` needs an interactive session
token — the server 403s a PAT outright (`require_session_token`). ``ask``
itself works with either credential; the CLI doesn't distinguish, it just
sends whatever `agnes auth` currently holds and renders a 401/403 the same
way as any other error.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_post, api_put
from cli.error_render import render_error

agent_app = typer.Typer(help="Manage agent profiles, scope, tokens, and one-shot asks")
scope_app = typer.Typer(help="Manage an agent's resource scope grants")
agent_app.add_typer(scope_app, name="scope")

# Server default (`CreateAgentTokenRequest.expires_in_days = 90`) mirrored
# here so the CLI's own default matches what omitting the flag would give
# you on the API directly.
_DEFAULT_TOKEN_EXPIRES_DAYS = 90

# Runtime `/responses` defaults (mirrors `app/api/agent_runtime.py`'s
# `_DEFAULT_TIMEOUT_S` / `_MAX_TIMEOUT_S`) — kept in sync manually since the
# CLI has no import-time dependency on the FastAPI router module.
_DEFAULT_ASK_TIMEOUT_S = 120
_MAX_ASK_TIMEOUT_S = 600
# Extra headroom on the HTTP client timeout over the server's own bounded
# wait, so the CLI's socket doesn't time out a hair before the server
# would have replied with its own 200/202.
_HTTP_TIMEOUT_MARGIN_S = 10.0
# Poll cadence for the background-job path. Deliberately short — job
# results for `agent_response` are typically ready within a few seconds
# once degraded to background.
_POLL_INTERVAL_S = 2.0

_TERMINAL_JOB_STATUSES = ("completed", "failed")


def _fail(resp) -> None:
    """Render an HTTP error response and exit non-zero.

    Uses the shared ``cli.error_render`` formatter so ``{"detail": {"code":
    ..., "message": ...}}`` bodies (every `_err()` raise in
    `agents_admin.py` / `agent_runtime.py`) render as a readable
    ``Error: <code> (HTTP <status>)`` block instead of a flattened dict dump.
    """
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    typer.echo(render_error(resp.status_code, body), err=True)
    raise typer.Exit(1)


def _resolve_agent(slug: str) -> dict:
    """Find an agent by slug via the list endpoint.

    The management API has no get-by-slug route (only get-by-id), so every
    slug-addressed subcommand pays one list round trip. Exits with a
    next-step hint on a miss, matching the command-UX standard's "not
    found must point forward" rule.
    """
    resp = api_get("/api/v1/agents")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json().get("data", [])
    for row in rows:
        if row.get("slug") == slug:
            return row
    typer.echo(f"Agent not found: {slug}. List your agents with: agnes agent list", err=True)
    raise typer.Exit(1)


def _print_agent(row: dict) -> None:
    typer.echo(f"id:                {row.get('id')}")
    typer.echo(f"slug:              {row.get('slug')}")
    typer.echo(f"name:              {row.get('name')}")
    if row.get("description"):
        typer.echo(f"description:       {row['description']}")
    typer.echo(f"model:             {row.get('model') or 'server default (no model policy)'}")
    typer.echo(
        f"token_budget:      {row.get('token_budget_monthly') if row.get('token_budget_monthly') is not None else '(unbounded)'}"
    )
    typer.echo(f"plugins_mode:      {row.get('plugins_mode')}")
    typer.echo(f"connections_mode:  {row.get('connections_mode')}")
    typer.echo(f"tables_mode:       {row.get('tables_mode')}")
    typer.echo(f"memory_mode:       {row.get('memory_mode')}")
    typer.echo(f"memory_write_mode: {row.get('memory_write_mode')}")
    typer.echo(f"is_default:        {row.get('is_default', False)}")
    typer.echo(f"created_at:        {row.get('created_at')}")


@agent_app.command("list")
def list_agents(as_json: bool = typer.Option(False, "--json")):
    """List your agent profiles."""
    resp = api_get("/api/v1/agents")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json().get("data", [])
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"Agents: {len(rows)}")
    if not rows:
        typer.echo("No agents yet. Create one with: agnes agent create <name> --slug <slug>")
        return
    slug_w = max(len("SLUG"), max((len(r.get("slug", "")) for r in rows), default=4))
    name_w = max(len("NAME"), max((len(r.get("name", "")) for r in rows), default=4))
    typer.echo(f"{'ID':<36}  {'SLUG':<{slug_w}}  {'NAME':<{name_w}}  MODEL")
    for r in rows:
        typer.echo(
            f"{r.get('id', ''):<36}  {r.get('slug', ''):<{slug_w}}  {r.get('name', ''):<{name_w}}  "
            f"{r.get('model') or 'server default (no model policy)'}"
        )


@agent_app.command("create")
def create_agent(
    name: str = typer.Argument(..., help="Display name"),
    slug: str = typer.Option(..., "--slug", help="Lowercase kebab-case unique id (immutable after creation)"),
    prompt_file: Optional[str] = typer.Option(
        None, "--prompt-file", help="Path to a file containing the system prompt"
    ),
    model: Optional[str] = typer.Option(
        None, "--model", help="Model override (omit for server default — no model policy)"
    ),
    budget: Optional[int] = typer.Option(None, "--budget", help="Monthly token budget"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Create a new agent profile.

    New agents default all four scope modes (plugins/connections/tables/
    memory) to `selected` server-side — use `agnes agent scope set` to grant
    specific resources.
    """
    payload: dict = {"name": name, "slug": slug}
    if prompt_file:
        path = Path(prompt_file)
        if not path.exists():
            typer.echo(f"Error: prompt file not found: {path}", err=True)
            raise typer.Exit(2)
        payload["system_prompt"] = path.read_text(encoding="utf-8").strip()
    if model is not None:
        payload["model"] = model
    if budget is not None:
        payload["token_budget_monthly"] = budget

    resp = api_post("/api/v1/agents", json=payload)
    if resp.status_code != 201:
        _fail(resp)
    row = resp.json()
    if as_json:
        typer.echo(json.dumps(row, indent=2))
        return
    typer.echo(f"Created agent id={row.get('id')} slug={row.get('slug')} name={row.get('name')}")


@agent_app.command("show")
def show_agent(
    slug: str = typer.Argument(..., help="Agent slug"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Show one agent profile's full detail."""
    row = _resolve_agent(slug)
    if as_json:
        typer.echo(json.dumps(row, indent=2))
        return
    _print_agent(row)


@agent_app.command("delete")
def delete_agent(
    slug: str = typer.Argument(..., help="Agent slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete an agent profile (and revoke every PAT minted for it). The
    default agent cannot be deleted — the server rejects that with
    `default_agent_undeletable`."""
    row = _resolve_agent(slug)
    if not yes:
        confirm = typer.confirm(f"Delete agent '{slug}' ({row['id']})?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/v1/agents/{row['id']}")
    if resp.status_code != 204:
        _fail(resp)
    typer.echo(f"Deleted agent {slug}")


@scope_app.command("set")
def scope_set(
    slug: str = typer.Argument(..., help="Agent slug"),
    plugin: list[str] = typer.Option([], "--plugin", help="Plugin id to grant (repeatable)"),
    table: list[str] = typer.Option([], "--table", help="Table id to grant (repeatable)"),
    connection: list[str] = typer.Option([], "--connection", help="Connection id to grant (repeatable)"),
    memory_domain: list[str] = typer.Option([], "--memory-domain", help="Memory domain id to grant (repeatable)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Replace an agent's resource scope grants.

    Every item passed here is a `(item_type, item_id)` pair PUT in a single
    call — the server replaces the full grant set, it does not merge. At
    least one of the four repeatable options is required.
    """
    items: list[dict] = []
    items += [{"item_type": "plugin", "item_id": v} for v in plugin]
    items += [{"item_type": "table", "item_id": v} for v in table]
    items += [{"item_type": "connection", "item_id": v} for v in connection]
    items += [{"item_type": "memory_domain", "item_id": v} for v in memory_domain]
    if not items:
        typer.echo(
            "Error: at least one of --plugin/--table/--connection/--memory-domain is required.",
            err=True,
        )
        raise typer.Exit(2)

    row = _resolve_agent(slug)
    resp = api_put(f"/api/v1/agents/{row['id']}/scope", json={"items": items})
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Scope set for agent {slug}: {len(body.get('items', []))} item(s)")


@agent_app.command("token")
def create_token(
    slug: str = typer.Argument(..., help="Agent slug"),
    name: str = typer.Option(..., "--name", help="Human label for the token"),
    expires_days: int = typer.Option(
        _DEFAULT_TOKEN_EXPIRES_DAYS,
        "--expires-days",
        help=f"Lifetime in days (default {_DEFAULT_TOKEN_EXPIRES_DAYS}); 0 = never expires",
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Print only the raw secret to stdout (for CI/scripts) — mirrors `agnes auth token create --raw`",
    ),
):
    """Mint an agent PAT.

    Requires all four scope modes (plugins/connections/tables/memory) to be
    `selected` — the server 403s with `agent_not_selected_mode` for an
    `all`-mode agent (including the default agent). Run `agnes agent scope
    set` first.
    """
    row = _resolve_agent(slug)
    payload = {"name": name, "expires_in_days": None if expires_days == 0 else expires_days}
    resp = api_post(f"/api/v1/agents/{row['id']}/tokens", json=payload)
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    if raw:
        typer.echo(
            "Agent personal access token created — this is shown ONCE and cannot be retrieved again.",
            err=True,
        )
        typer.echo(data["token"])
        return
    typer.echo("Agent personal access token created — this is shown ONCE and cannot be retrieved again:")
    typer.echo("")
    typer.echo(f"    {data['token']}")
    typer.echo("")
    typer.echo(f"id:      {data['id']}")
    typer.echo(f"name:    {data['name']}")
    typer.echo(f"agent:   {slug}")
    typer.echo(f"expires: {data.get('expires_at') or 'never'}")


def _render_ask_answer(body: dict, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(body.get("answer") or "(no answer)")


def _render_job_result(job: dict, as_json: bool) -> None:
    if job.get("status") == "failed":
        err = job.get("error")
        if isinstance(err, dict):
            typer.echo(f"Job failed: {err.get('code', 'error')}: {err.get('message', '')}", err=True)
        else:
            typer.echo(f"Job failed: {err}", err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(job, indent=2))
        return
    result = job.get("result") or {}
    typer.echo(result.get("answer") or "(no answer)")


def _poll_job(job_id: str, timeout_s: float) -> dict:
    """Poll `GET /api/v1/jobs/{job_id}` until it reaches a terminal status or
    `timeout_s` elapses.

    Bounded two ways: primarily by a wall-clock deadline (`time.monotonic()
    at entry + timeout_s`) so the budget the caller passed in is what
    actually gets spent, and secondarily by attempt count (`timeout_s //
    _POLL_INTERVAL_S`, floor 1) so a mocked/no-op `time.sleep` in tests (or a
    genuinely instant server) can't turn this into an unbounded hot loop —
    attempts still happen within the deadline, they just can't out-count it.
    """
    deadline = time.monotonic() + timeout_s
    max_attempts = max(1, int(timeout_s // _POLL_INTERVAL_S) + 1)
    job: dict = {}
    for attempt in range(max_attempts):
        resp = api_get(f"/api/v1/jobs/{job_id}")
        if resp.status_code != 200:
            _fail(resp)
        job = resp.json()
        if job.get("status") in _TERMINAL_JOB_STATUSES:
            return job
        if time.monotonic() >= deadline:
            break
        if attempt < max_attempts - 1:
            time.sleep(_POLL_INTERVAL_S)
    typer.echo(
        f"Timed out after {timeout_s:.0f}s waiting for job {job_id} "
        f"(status={job.get('status')}). The run keeps going server-side — "
        f"it is not cancelled by this timeout. Check it later with a fresh "
        f"`agnes agent ask` call, or inspect `GET /api/v1/jobs/{job_id}` directly.",
        err=True,
    )
    raise typer.Exit(1)


@agent_app.command("ask")
def ask(
    slug: str = typer.Argument(..., help="Agent slug"),
    prompt: str = typer.Argument(..., help="Prompt to send"),
    timeout: Optional[int] = typer.Option(
        None,
        "--timeout",
        help=(
            f"Max seconds for the synchronous wait (default {_DEFAULT_ASK_TIMEOUT_S}, "
            f"capped at {_MAX_ASK_TIMEOUT_S} for the sync leg — background job polling "
            "continues for the full --timeout value)"
        ),
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """One-shot request/response over an agent (`POST /responses`).

    A `200` is the answer, ready now. A `202` means the sync wait outran
    the timeout (or the server degraded immediately) — this command polls
    `GET /api/v1/jobs/{id}` for the *remainder* of the `--timeout` budget
    (the sync POST above may itself have consumed a chunk of it), so the
    total wall-clock time this command can spend stays bounded by
    `--timeout` instead of doubling to (sync wait) + (full poll budget).
    The underlying run is never cancelled by a client-side timeout; only
    the CLI's own wait is bounded.
    """
    start = time.monotonic()
    total_timeout = _DEFAULT_ASK_TIMEOUT_S if timeout is None else max(1, timeout)
    server_timeout = min(total_timeout, _MAX_ASK_TIMEOUT_S)
    resp = api_post(
        f"/api/v1/agents/{slug}/responses",
        json={"input": prompt, "timeout_s": server_timeout},
        timeout=server_timeout + _HTTP_TIMEOUT_MARGIN_S,
    )
    if resp.status_code == 200:
        _render_ask_answer(resp.json(), as_json)
        return
    if resp.status_code == 202:
        job_id = resp.json()["job_id"]
        remaining = max(1, total_timeout - int(time.monotonic() - start))
        job = _poll_job(job_id, remaining)
        _render_job_result(job, as_json)
        return
    _fail(resp)
