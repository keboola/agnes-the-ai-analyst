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
import typer

mcp_app = typer.Typer(
    help="Start Agnes MCP server for Claude Desktop (stdio transport)",
    invoke_without_command=True,
)


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
