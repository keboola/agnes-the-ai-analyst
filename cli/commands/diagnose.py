"""Diagnose command — agnes diagnose."""

import json
from pathlib import Path

import typer

from cli.client import api_get
from cli.config import get_sync_state
from cli.lib.session_health import capture_session_health

diagnose_app = typer.Typer(help="System diagnostics")


@diagnose_app.callback(invoke_without_command=True)
def diagnose(
    ctx: typer.Context,
    symptom: str = typer.Option(None, "--symptom", help="Describe the problem"),
    component: str = typer.Option(None, "--component", help="Check specific component"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
    include_schema: bool = typer.Option(
        False,
        "--include-schema",
        help=(
            "Include the DB schema-version check. Off by default since the "
            "answer is rarely actionable on a healthy instance and shows up "
            "as noise in the agent-facing output (issue #204). On when the "
            "operator is verifying a migration."
        ),
    ),
):
    """Run comprehensive system diagnostics. AI-agent friendly output."""
    # If a subcommand was invoked (e.g. `agnes diagnose system`), defer to it
    # rather than running the default whole-system diagnostic.
    if ctx.invoked_subcommand is not None:
        return

    checks = []

    # 1. API reachability
    try:
        resp = api_get("/api/health")
        health = resp.json()
        checks.append({"name": "api", "status": "ok", "latency_ms": resp.elapsed.total_seconds() * 1000})

        # Detailed health (auth required) for service-level checks
        try:
            params = {"include": "schema"} if include_schema else None
            resp_d = api_get("/api/health/detailed", params=params)
            detailed = resp_d.json()
            for svc_name, svc_data in detailed.get("services", {}).items():
                check = {"name": svc_name, "status": svc_data.get("status", "unknown")}
                check.update({k: v for k, v in svc_data.items() if k != "status"})
                checks.append(check)
        except Exception:
            # Auth may not be configured — minimal reachability is sufficient
            pass
    except Exception as e:
        checks.append({"name": "api", "status": "error", "detail": str(e)})

    # Issue #244: detect silently-broken capture-session by comparing
    # observed SessionStart files against the uploaded-log entries.
    # Adds one entry to `checks` with status ok / warning / info.
    try:
        checks.append(capture_session_health(Path.cwd()))
    except Exception as e:
        checks.append({"name": "capture-session", "status": "info", "detail": f"health check failed: {e}"})

    # Determine overall — `info` and `unknown` surface in the per-check
    # output but never promote the headline (issue #178).
    overall = "healthy"
    for c in checks:
        if c["status"] == "error":
            overall = "unhealthy"
            break
        if c["status"] == "warning":
            overall = "degraded"

    # Generate suggested actions
    actions = []
    for c in checks:
        if c["status"] == "error" and c["name"] == "api":
            actions.append("Server unreachable. Check: docker compose ps, agnes server logs")
        if c.get("stale_tables"):
            for t in c["stale_tables"]:
                actions.append(f"Table '{t}' is stale. Run: agnes server logs scheduler | grep {t}")
        if c["name"] == "capture-session" and c["status"] == "warning":
            actions.append(
                "Capture-session may be silently failing. Run "
                "`agnes capture-session --verbose < ~/.claude/projects/<encoded>/<session>.jsonl` "
                "against a recent session file to surface the real error."
            )

    result = {
        "overall": overall,
        "checks": checks,
        "suggested_actions": actions,
    }

    if as_json:
        typer.echo(json.dumps(result, indent=2))
    else:
        typer.echo(f"Overall: {overall}")
        for c in checks:
            detail = ""
            if "detail" in c:
                detail = f" — {c['detail']}"
            if "tables" in c:
                detail = f" ({c['tables']} tables)"
            if "latency_ms" in c:
                detail = f" ({c['latency_ms']:.0f}ms)"
            typer.echo(f"  [{c['status']:7s}] {c['name']}{detail}")
        if actions:
            typer.echo("\nSuggested actions:")
            for a in actions:
                typer.echo(f"  - {a}")


@diagnose_app.command("system")
def system_status(
    local: bool = typer.Option(False, "--local", help="Show local-only status (no server)"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show server-side health status (was `agnes status` pre-clean-bootstrap).

    Reports server reachability and per-service health. Use `agnes status` for
    workspace-side state (initialized? data fresh?).
    """
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
            typer.echo("Mode: offline (local data)")
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
