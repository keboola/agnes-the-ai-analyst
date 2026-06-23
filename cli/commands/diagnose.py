"""Diagnose command — agnes diagnose."""

import json
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get
from cli.config import get_sync_state
from cli.lib.jira_partition_check import detect_jira_partition_layout
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
    include_operator_checks: bool = typer.Option(
        False,
        "--include-operator-checks",
        help=(
            "Aggregate the headline status across operator-side checks "
            "(stale tables, session-pipeline cadence, BQ billing config) "
            "in addition to analyst-side ones. Default off when the caller "
            "is an analyst — those checks aren't actionable from a fresh "
            "analyst install and reading `Overall: degraded` on first run "
            "erodes trust in the install (issue #345 B). Admins/operators "
            "auto-promote to the full headline based on the server-reported "
            "caller_role."
        ),
    ),
):
    """Run comprehensive system diagnostics. AI-agent friendly output."""
    # If a subcommand was invoked (e.g. `agnes diagnose system`), defer to it
    # rather than running the default whole-system diagnostic.
    if ctx.invoked_subcommand is not None:
        return

    checks = []
    # ``caller_role`` is present only on servers shipping the
    # role-aware health fields (issue #345 B). Legacy servers don't
    # ship it; absent role disables audience filtering so we don't
    # regress against an older server with the full-aggregation
    # contract the rest of the CLI was written against.
    caller_role: Optional[str] = None

    # 1. API reachability
    try:
        resp = api_get("/api/health")
        health = resp.json()
        checks.append({"name": "api", "status": "ok", "audience": "analyst", "latency_ms": resp.elapsed.total_seconds() * 1000})

        # Detailed health (auth required) for service-level checks
        try:
            params = {"include": "schema"} if include_schema else None
            resp_d = api_get("/api/health/detailed", params=params)
            detailed = resp_d.json()
            if "caller_role" in detailed:
                caller_role = detailed["caller_role"]
            for svc_name, svc_data in detailed.get("services", {}).items():
                check = {"name": svc_name, "status": svc_data.get("status", "unknown")}
                check.update({k: v for k, v in svc_data.items() if k != "status"})
                checks.append(check)
        except Exception:
            # Auth may not be configured — minimal reachability is sufficient
            pass
    except Exception as e:
        checks.append({"name": "api", "status": "error", "audience": "analyst", "detail": str(e)})

    # Issue #244: detect silently-broken capture-session by comparing
    # observed SessionStart files against the uploaded-log entries.
    # Adds one entry to `checks` with status ok / warning / info.
    try:
        cap = capture_session_health(Path.cwd())
        cap.setdefault("audience", "analyst")
        checks.append(cap)
    except Exception as e:
        checks.append({"name": "capture-session", "status": "info", "audience": "analyst", "detail": f"health check failed: {e}"})

    # Issue #394: detect Jira partition layout (flat YYYY-MM vs hive month=*/).
    # Resolves the Jira data directory from the DATA_DIR env var (mirrors how
    # the connector itself locates its output); defaults to /data/extracts/jira.
    # Operator-only audience — analysts can't act on a partition migration.
    try:
        import os
        _data_root = Path(os.environ.get("DATA_DIR", "/data"))
        _jira_dir = _data_root / "extracts" / "jira"
        jira_check = detect_jira_partition_layout(_jira_dir)
        jira_check.setdefault("audience", "operator")
        checks.append(jira_check)
    except Exception as e:
        checks.append({"name": "jira-partition-format", "status": "info", "audience": "operator", "detail": f"partition check failed: {e}"})

    # Determine overall — `info` and `unknown` surface in the per-check
    # output but never promote the headline (issue #178).
    #
    # Audience-aware headline (issue #345 B): when the server reports a
    # ``caller_role``, analysts see analyst-only aggregation by default;
    # operators auto-promote to the full headline; analysts can manually
    # opt in via ``--include-operator-checks``. Legacy servers that don't
    # ship ``caller_role`` keep the original full-aggregation behaviour
    # — no analyst-only filtering until the server tags checks.
    role_aware = caller_role is not None
    operator_mode = (not role_aware) or include_operator_checks or caller_role != "analyst"
    relevant = checks if operator_mode else [c for c in checks if c.get("audience") == "analyst"]
    overall = "healthy"
    for c in relevant:
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
        "caller_role": caller_role,
        "checks": checks,
        "suggested_actions": actions,
    }

    if as_json:
        typer.echo(json.dumps(result, indent=2))
    else:
        # When analysts are filtered to analyst-only aggregation, surface
        # any operator-side warnings as a secondary line so they're not
        # invisible — they just don't get to drive the headline.
        operator_warns = [
            c for c in checks
            if c.get("audience") == "operator" and c.get("status") in ("warning", "error")
        ]
        if not operator_mode and operator_warns:
            typer.echo(
                f"Overall: {overall} (analyst-side); "
                f"{len(operator_warns)} operator-side "
                f"{'warning' if len(operator_warns) == 1 else 'warnings'}"
            )
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
