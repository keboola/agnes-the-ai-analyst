"""Auth commands — da login, da logout, da whoami."""

import typer

from cli.client import api_post, api_get
from cli.config import save_token, clear_token, get_token, get_server_url

auth_app = typer.Typer(help="Authentication commands")


@auth_app.command()
def login(
    email: str = typer.Option(..., prompt=True, help="Your email address"),
    password: str = typer.Option(
        "", prompt="Password (leave empty for magic-link / OAuth accounts)",
        hide_input=True, help="Your password (if the account has one)",
    ),
    server: str = typer.Option(None, help="Server URL override"),
):
    """Login and obtain a JWT token.

    Password-enabled accounts: enter the password when prompted.
    Magic-link / OAuth accounts: leave the password empty — the server will
    respond with guidance pointing you to the correct auth provider.
    """
    if server:
        import os
        os.environ["DA_SERVER"] = server

    body = {"email": email}
    if password:
        body["password"] = password

    try:
        resp = api_post("/auth/token", json=body)
        if resp.status_code == 200:
            data = resp.json()
            save_token(data["access_token"], data["email"], data["role"])
            typer.echo(f"Logged in as {data['email']} (role: {data['role']})")
            return
        # Helpful error for accounts that cannot login via password.
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        if resp.status_code == 401 and "external authentication" in str(detail).lower():
            typer.echo(
                "This account uses a magic link / OAuth provider. "
                "Sign in via the web UI, open /profile, and create a personal "
                "access token — then export it as DA_TOKEN.",
                err=True,
            )
        else:
            typer.echo(f"Login failed: {detail}", err=True)
        raise typer.Exit(1)
    except typer.Exit:
        raise
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
