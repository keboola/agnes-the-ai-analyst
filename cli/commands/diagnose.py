"""Diagnose command — da diagnose."""

import json

import typer

from cli.client import api_get

diagnose_app = typer.Typer(help="System diagnostics")


@diagnose_app.callback(invoke_without_command=True)
def diagnose(
    symptom: str = typer.Option(None, "--symptom", help="Describe the problem"),
    component: str = typer.Option(None, "--component", help="Check specific component"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Run comprehensive system diagnostics. AI-agent friendly output."""
    checks = []

    # 1. API reachability
    try:
        resp = api_get("/api/health")
        health = resp.json()
        checks.append({"name": "api", "status": "ok", "latency_ms": resp.elapsed.total_seconds() * 1000})

        # Detailed health (auth required) for service-level checks
        try:
            resp_d = api_get("/api/health/detailed")
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

    # Determine overall
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
            actions.append("Server unreachable. Check: docker compose ps, da server logs")
        if c.get("stale_tables"):
            for t in c["stale_tables"]:
                actions.append(f"Table '{t}' is stale. Run: da server logs scheduler | grep {t}")

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
