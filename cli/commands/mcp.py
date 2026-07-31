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

import time
import webbrowser
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_post, api_put
from cli.config import get_server_url

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
        ...,
        help="MCP source id (src_*) — find it with 'agnes catalog' or admin UI",
    ),
    value: Optional[str] = typer.Option(
        None,
        "--value",
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


@my_secret_app.command("test")
def my_secret_test(
    source_id: str = typer.Argument(..., help="MCP source id (src_*)"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Verify your stored credential works against the upstream source."""
    import json

    resp = api_post(f"/api/mcp/sources/{source_id}/my-secret/test")
    if resp.status_code != 200:
        # 403 carries the connect remedy; _fail prints it and hints the next step.
        _fail(resp)
    body = resp.json() or {}
    if json_out:
        typer.echo(json.dumps(body))
        return
    if body.get("ok"):
        typer.echo(f"ok — {body.get('tool_count')} tools reachable")
    else:
        typer.echo(f"not working: {body.get('message')}", err=True)
        typer.echo(f"Reconnect with: agnes mcp my-secret set {source_id}", err=True)


# Polling cadence for `agnes mcp connect` — matches the spec's "device-style
# UX": open the authorize URL, then poll status every few seconds until the
# browser-driven flow completes (or the caller gives up waiting).
_CONNECT_POLL_INTERVAL_SECONDS = 3
_CONNECT_POLL_TIMEOUT_SECONDS = 120


@mcp_app.command("connect")
def mcp_connect(
    source_id: str = typer.Argument(
        ...,
        help="MCP source id (src_*) with auth_method='oauth' — find it with 'agnes catalog' or admin UI",
    ),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the authorize URL instead of opening a browser"),
    timeout: int = typer.Option(
        _CONNECT_POLL_TIMEOUT_SECONDS, "--timeout", help="Seconds to wait for the browser flow to complete"
    ),
):
    """Connect your account to an OAuth-authenticated MCP source.

    Opens ``GET /api/mcp/sources/{id}/oauth/authorize`` in your default
    browser — the same URL the Connect button on ``/me/connections`` uses —
    then polls ``GET .../my-secret`` every few seconds until it reports
    ``has_secret: true`` or ``--timeout`` elapses (device-style UX, same
    shape as a CLI OAuth device flow). Your browser must already carry a
    logged-in Agnes session; this command never sees or forwards your CLI
    token to the browser.
    """
    url = f"{get_server_url()}/api/mcp/sources/{source_id}/oauth/authorize"
    opened = False if no_browser else webbrowser.open(url)
    if opened:
        typer.echo(f"Opened your browser to connect {source_id}. Waiting for you to finish there…")
    else:
        typer.echo(f"Open this URL in your browser to connect {source_id}:\n  {url}")

    deadline = time.monotonic() + timeout
    while True:
        resp = api_get(f"/api/mcp/sources/{source_id}/my-secret")
        if resp.status_code == 200 and (resp.json() or {}).get("has_secret"):
            typer.echo(f"Connected {source_id}.")
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(_CONNECT_POLL_INTERVAL_SECONDS)
    typer.echo(
        f"Timed out waiting for the connection to {source_id}. Finish the browser flow and run "
        f"`agnes mcp connect {source_id}` again, or check `agnes mcp my-secret status {source_id}`.",
        err=True,
    )
    raise typer.Exit(1)


@mcp_app.command("disconnect")
def mcp_disconnect(
    source_id: str = typer.Argument(..., help="MCP source id (src_*)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Disconnect your OAuth connection for a source.

    Drops your stored token server-side (``DELETE .../oauth/connection``).
    This does not revoke the token upstream — do that in the source's own
    system if you want it fully dead.
    """
    if not yes:
        if not typer.confirm(f"Disconnect your connection to {source_id}?"):
            raise typer.Abort()
    resp = api_delete(f"/api/mcp/sources/{source_id}/oauth/connection")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Disconnected {source_id}.")
