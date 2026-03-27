"""Auth commands — da login, da logout, da whoami."""

import typer

from cli.client import api_post, api_get
from cli.config import save_token, clear_token, get_token, get_server_url

auth_app = typer.Typer(help="Authentication commands")


@auth_app.command()
def login(
    email: str = typer.Option(..., prompt=True, help="Your email address"),
    server: str = typer.Option(None, help="Server URL override"),
):
    """Login and obtain a JWT token."""
    if server:
        import os
        os.environ["DA_SERVER"] = server

    try:
        resp = api_post("/auth/token", json={"email": email})
        if resp.status_code == 200:
            data = resp.json()
            save_token(data["access_token"], data["email"], data["role"])
            typer.echo(f"Logged in as {data['email']} (role: {data['role']})")
        else:
            typer.echo(f"Login failed: {resp.json().get('detail', resp.text)}", err=True)
            raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Connection error: {e}", err=True)
        raise typer.Exit(1)


@auth_app.command()
def logout():
    """Clear stored token."""
    clear_token()
    typer.echo("Logged out.")


@auth_app.command()
def whoami():
    """Show current user info."""
    token = get_token()
    if not token:
        typer.echo("Not logged in. Run: da login")
        raise typer.Exit(1)

    import jwt
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        typer.echo(f"Email: {payload.get('email', 'unknown')}")
        typer.echo(f"Role:  {payload.get('role', 'unknown')}")
        typer.echo(f"Server: {get_server_url()}")
    except Exception:
        typer.echo("Invalid token. Run: da login")
        raise typer.Exit(1)
