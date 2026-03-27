"""Server management commands — da server deploy/rollback/logs/status/backup."""

import subprocess
import sys

import typer

server_app = typer.Typer(help="Server management (Docker/Kamal operations)")


@server_app.command("status")
def server_status():
    """Show Docker container status."""
    _run("docker compose ps")


@server_app.command("logs")
def server_logs(
    service: str = typer.Argument("app", help="Service name: app, scheduler, telegram-bot"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    since: str = typer.Option("1h", "--since", help="Show logs since duration"),
    lines: int = typer.Option(100, "--tail", "-n", help="Number of lines"),
):
    """Show logs for a Docker service."""
    cmd = f"docker compose logs {service} --tail {lines}"
    if follow:
        cmd += " --follow"
    _run(cmd)


@server_app.command("restart")
def server_restart(
    service: str = typer.Argument("app", help="Service to restart"),
):
    """Restart a Docker service."""
    _run(f"docker compose restart {service}")
    typer.echo(f"Restarted {service}")


@server_app.command("deploy")
def server_deploy(
    staging: bool = typer.Option(False, "--staging", help="Deploy to staging"),
):
    """Deploy using Kamal (or docker compose pull + up)."""
    if staging:
        _run("kamal deploy -d staging")
    else:
        _run("kamal deploy")


@server_app.command("rollback")
def server_rollback():
    """Rollback to previous deployment."""
    _run("kamal rollback")


@server_app.command("backup")
def server_backup(
    output: str = typer.Option("./backup", help="Backup output directory"),
):
    """Backup server data (DuckDB + parquet files)."""
    import shutil
    from pathlib import Path
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(output) / f"backup_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Copy DuckDB state via docker cp
    _run(f"docker compose cp app:/data/state/ {backup_dir}/state/", check=False)
    typer.echo(f"Backup saved to {backup_dir}")


def _run(cmd: str, check: bool = True):
    """Run a shell command, streaming output."""
    try:
        result = subprocess.run(cmd, shell=True, text=True, capture_output=False)
        if check and result.returncode != 0:
            raise typer.Exit(result.returncode)
    except FileNotFoundError:
        typer.echo(f"Command not found. Is Docker/Kamal installed?", err=True)
        raise typer.Exit(1)
