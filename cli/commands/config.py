"""`agnes config` — read/write the CLI config (``config.yaml``)."""

from __future__ import annotations

import typer

from cli.config import save_config

config_app = typer.Typer(
    name="config",
    help="Read/write agnes CLI config (server URL, …).",
    no_args_is_help=True,
)


@config_app.command("set-server")
def set_server(
    url: str = typer.Argument(..., help="Agnes server URL to record in config.yaml"),
) -> None:
    """Set the server URL in ``config.yaml``, MERGING (never clobbering other
    keys such as ``workspace_root``).

    ``save_config`` does a load → update → dump merge, so a re-run of the
    installer / this command on an already-initialized machine keeps the
    workspace anchor and any other keys intact — unlike a naive ``cat >
    config.yaml`` which truncates the file.
    """
    save_config({"server": url})
    typer.echo(f"agnes config: server = {url}")
