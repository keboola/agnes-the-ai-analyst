"""Auth commands — da login, da logout, da whoami, da auth import-token."""

import httpx
import typer

from cli.client import api_post, api_get
from cli.config import (
    save_token,
    clear_token,
    get_token,
    get_server_url,
    save_config,
    load_config,
)

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


@auth_app.command("import-token")
def import_token(
    token: str = typer.Option(..., "--token", help="JWT / Personal Access Token to import"),
    server: str = typer.Option(
        None,
        "--server",
        help="Server URL (defaults to ~/.config/da/config.yaml or $DA_SERVER)",
    ),
    email: str = typer.Option(
        None,
        "--email",
        help="Override email (used only if the JWT lacks an 'email' claim)",
    ),
    role: str = typer.Option(
        None,
        "--role",
        help="Override role (used only if the JWT lacks a 'role' claim)",
    ),
    skip_verify: bool = typer.Option(
        False,
        "--skip-verify",
        help="Skip the server-side verification step (offline import)",
    ),
):
    """Import a personal access token non-interactively.

    Decodes the JWT locally to extract the email/role claims, verifies it
    against the server, and writes it to ~/.config/da/token.json using the
    canonical format so subsequent `da auth whoami` / `da sync` calls
    authenticate cleanly.

    Example:

        da auth import-token --token "$AGNES_PAT"
        da auth import-token --token "$AGNES_PAT" --server https://agnes.example.com
    """
    import os
    import jwt as pyjwt

    # 1) Seed server URL so the verify call below uses the right base URL.
    if server:
        save_config({"server": server})
        os.environ["DA_SERVER"] = server
    else:
        cfg = load_config()
        if not os.environ.get("DA_SERVER") and not cfg.get("server"):
            typer.echo(
                "No server configured. Pass --server https://<host> or set "
                "DA_SERVER, or seed ~/.config/da/config.yaml first.",
                err=True,
            )
            raise typer.Exit(1)

    # 2) Decode JWT without signature verification — we only need the claims.
    resolved_email = email
    resolved_role = role
    try:
        payload = pyjwt.decode(token, options={"verify_signature": False})
        resolved_email = resolved_email or payload.get("email")
        resolved_role = resolved_role or payload.get("role")
    except Exception as e:
        typer.echo(f"Could not decode token as JWT: {e}", err=True)
        raise typer.Exit(1)

    # 3) Server-side verification. The server has no dedicated /auth/me — we
    #    use /api/catalog/tables which is the lightest endpoint that every
    #    authenticated user can call and also exercises the PAT validation
    #    path (revocation, expiry, token_hash match).
    verify_url = get_server_url()
    if not skip_verify:
        headers = {"Authorization": f"Bearer {token}"}
        try:
            with httpx.Client(base_url=verify_url, headers=headers, timeout=15.0) as client:
                resp = client.get("/api/catalog/tables")
        except Exception as e:
            typer.echo(f"Could not reach server {verify_url}: {e}", err=True)
            raise typer.Exit(1)
        if resp.status_code == 401:
            detail = "unauthorized"
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            typer.echo(f"Token rejected by server ({verify_url}): {detail}", err=True)
            raise typer.Exit(1)
        if resp.status_code >= 500:
            typer.echo(
                f"Server error from {verify_url} during verification "
                f"(HTTP {resp.status_code}). Re-run with --skip-verify to bypass.",
                err=True,
            )
            raise typer.Exit(1)
        # 4) Fallback claim lookup via a response the server might include.
        #    /api/catalog/tables doesn't return user info, but other JWT
        #    issuers might later gain an /auth/me. For now, we rely on JWT
        #    claims + the CLI overrides.

    # 5) If we still lack email/role, refuse rather than writing a partial record.
    if not resolved_email or not resolved_role:
        typer.echo(
            "Token is missing 'email' and/or 'role' claims. Re-issue the token "
            "or pass --email and --role explicitly.",
            err=True,
        )
        raise typer.Exit(1)

    # 6) Persist in the canonical on-disk format used by cli/config.py.
    save_token(token, resolved_email, resolved_role)
    typer.echo(f"Imported token for {resolved_email} (role: {resolved_role}).")


from cli.commands.tokens import token_app
auth_app.add_typer(token_app, name="token")
