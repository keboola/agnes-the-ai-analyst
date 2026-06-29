"""`agnes mcp` — start the Agnes MCP server (stdio transport).

The MCP server exposes Agnes data tools (catalog, schema, describe, query,
pull) to Claude Desktop via the Model Context Protocol.  Claude Desktop
launches this as a subprocess and communicates over stdin/stdout.

Unlike the Bash tool inside Claude Desktop, MCP subprocesses run outside
the sandbox with full network access — so tools like ``catalog`` and
``query`` can reach the Agnes server at localhost:8000.

Configured automatically by the Cowork bundle's setup.py, which detects the
agnes binary path and writes it into .claude/settings.json:

    {
      "mcpServers": {
        "agnes": {
          "command": "/Users/you/.local/bin/agnes",
          "args": ["mcp"],
          "type": "stdio"
        }
      }
    }
"""
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_put

mcp_app = typer.Typer(
    help="Start Agnes MCP server for Claude Desktop (stdio transport)",
    invoke_without_command=True,
)


my_secret_app = typer.Typer(
    help="Manage your own per-user secret for an MCP source (RFC #461 §4)",
)
mcp_app.add_typer(my_secret_app, name="my-secret")


@mcp_app.callback(invoke_without_command=True)
def mcp_command(ctx: typer.Context) -> None:
    """Start the Agnes MCP server.

    Claude Desktop discovers and launches this automatically when the
    Cowork workspace is opened.  You don't need to run it manually.

    For diagnostics:
        agnes mcp          # starts the server; Ctrl-C to stop
    """
    if ctx.invoked_subcommand is not None:
        return

    try:
        from cli.mcp.server import run
    except ImportError as exc:
        typer.echo(
            f"Error: MCP server requires the 'mcp' package.\n"
            f"Install it with: uv pip install 'mcp>=1.0'\n"
            f"Details: {exc}",
            err=True,
        )
        raise typer.Exit(1)

    run()


def _fail(resp) -> None:
    """Print server error body to stderr and exit with the resp status code."""
    body = ""
    try:
        body = resp.text
    except Exception:
        pass
    typer.echo(f"HTTP {resp.status_code}: {body}", err=True)
    raise typer.Exit(1)


@my_secret_app.command("set")
def my_secret_set(
    source_id: str = typer.Argument(
        ..., help="MCP source id (src_*) — find it with 'agnes catalog' or admin UI",
    ),
    value: Optional[str] = typer.Option(
        None, "--value",
        help="Secret value. Omit to read one line from stdin (keeps it out of shell history).",
    ),
):
    """Store your per-user credential for a per_user-scoped MCP source.

    Encrypted at rest on the server in ``mcp_user_secrets``. Never
    transmitted back to the client — rotation is write-only.
    """
    if value is None:
        import sys
        value = sys.stdin.readline().rstrip("\n")
    if not value:
        typer.echo("set: secret value is empty — refusing.", err=True)
        raise typer.Exit(2)
    resp = api_put(f"/api/mcp/sources/{source_id}/my-secret", json={"value": value})
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Stored your per-user secret for source {source_id}.")


@my_secret_app.command("clear")
def my_secret_clear(
    source_id: str = typer.Argument(..., help="MCP source id (src_*)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Drop your per-user secret. For per_user sources the next call
    falls back to the shared vault path."""
    if not yes:
        if not typer.confirm(f"Clear your per-user secret for {source_id}?"):
            raise typer.Abort()
    resp = api_delete(f"/api/mcp/sources/{source_id}/my-secret")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Cleared your per-user secret for source {source_id}.")


@my_secret_app.command("status")
def my_secret_status(
    source_id: str = typer.Argument(..., help="MCP source id (src_*)"),
):
    """Show whether you have a per-user secret stored + the source's scope."""
    resp = api_get(f"/api/mcp/sources/{source_id}/my-secret")
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}
    has = body.get("has_secret", False)
    scope = body.get("source_scope", "?")
    typer.echo(f"source={source_id} scope={scope} has_secret={'yes' if has else 'no'}")
