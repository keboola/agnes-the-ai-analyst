"""`agnes profile` — self-service profile management."""

from __future__ import annotations

import typer

from cli.client import api_patch

profile_app = typer.Typer(
    name="profile",
    help="Manage your Agnes profile.",
    no_args_is_help=True,
)


@profile_app.command("set-name")
def set_name(
    name: str = typer.Argument(..., help="New display name to set on your account."),
) -> None:
    """Update your display name on the Agnes server.

    Email stays read-only — it is the identity key from the auth provider.
    Google Workspace sync only sets the name at first sign-in, so a name
    you set here is never overwritten by a subsequent sync run.

    Example::

        agnes profile set-name "Alice Smith"
    """
    resp = api_patch("/api/me/display-name", json={"name": name})
    if resp.status_code == 200:
        data = resp.json()
        typer.echo(f"Display name updated: {data.get('name', name)!r}")
    else:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Error {resp.status_code}: {detail}", err=True)
        raise typer.Exit(1)
