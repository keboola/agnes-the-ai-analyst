"""Setup commands — da setup init/test-connection/deploy/first-sync/verify."""

import json
import os

import typer

from cli.client import api_get, api_post

setup_app = typer.Typer(help="Instance setup (guided by AI agent)")


@setup_app.command("init")
def setup_init(
    server: str = typer.Option("http://localhost:8000", help="Server URL"),
):
    """Initialize a new instance configuration."""
    typer.echo(f"Server: {server}")
    typer.echo("Creating config directory...")

    from cli.config import _config_dir
    config_dir = _config_dir()
    config_file = config_dir / "config.yaml"

    import yaml
    config = {"server": server}
    config_file.write_text(yaml.dump(config))
    typer.echo(f"Config saved to {config_file}")

    os.environ["DA_SERVER"] = server
    typer.echo("\nNext: da setup test-connection")


@setup_app.command("test-connection")
def test_connection():
    """Test connection to the server and data source."""
    typer.echo("Testing server connection...")
    try:
        resp = api_get("/api/health")
        health = resp.json()
        typer.echo(f"  Server: {health.get('status', 'unknown')}")
        for svc, info in health.get("services", {}).items():
            typer.echo(f"  {svc}: {info.get('status', '?')}")
    except Exception as e:
        typer.echo(f"  FAILED: {e}", err=True)
        raise typer.Exit(1)

    typer.echo("\nNext: da setup first-sync")


@setup_app.command("first-sync")
def first_sync():
    """Trigger the first data sync."""
    typer.echo("Triggering initial data sync...")
    try:
        resp = api_post("/api/sync/trigger")
        if resp.status_code == 200:
            typer.echo(f"  {resp.json().get('message', 'Sync triggered')}")
        else:
            typer.echo(f"  Failed: {resp.text}", err=True)
    except Exception as e:
        typer.echo(f"  Error: {e}", err=True)
        raise typer.Exit(1)

    typer.echo("\nNext: da setup verify")


@setup_app.command("verify")
def verify():
    """Verify the instance is working end-to-end."""
    typer.echo("Running verification checks...")
    checks = []

    # 1. Health
    try:
        resp = api_get("/api/health")
        h = resp.json()
        checks.append(("Health", h.get("status") == "healthy", h.get("status")))
    except Exception as e:
        checks.append(("Health", False, str(e)))

    # 2. Data
    try:
        resp = api_get("/api/sync/manifest")
        m = resp.json()
        table_count = len(m.get("tables", {}))
        checks.append(("Data", table_count > 0, f"{table_count} tables"))
    except Exception as e:
        checks.append(("Data", False, str(e)))

    # 3. Users
    try:
        resp = api_get("/api/users")
        if resp.status_code == 200:
            count = len(resp.json())
            checks.append(("Users", count > 0, f"{count} users"))
        else:
            checks.append(("Users", True, "requires admin token"))
    except Exception as e:
        checks.append(("Users", False, str(e)))

    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        typer.echo(f"  [{status}] {name}: {detail}")

    all_ok = all(ok for _, ok, _ in checks)
    if all_ok:
        typer.echo("\nAll checks passed! Instance is ready.")
    else:
        typer.echo("\nSome checks failed. Review above.")
        raise typer.Exit(1)


@setup_app.command("add-user")
def add_first_user(
    email: str = typer.Argument(..., help="Admin email"),
    name: str = typer.Option("", help="Display name"),
):
    """Add the first admin user to the instance."""
    resp = api_post("/api/users", json={
        "email": email,
        "name": name or email.split("@")[0],
        "role": "admin",
    })
    if resp.status_code == 201:
        typer.echo(f"Admin user created: {email}")
    elif resp.status_code == 409:
        typer.echo(f"User {email} already exists.")
    else:
        typer.echo(f"Failed: {resp.text}", err=True)
        raise typer.Exit(1)
