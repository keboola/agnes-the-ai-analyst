"""``agnes docs`` — read in-app documentation from the terminal.

Mirrors the server-rendered ``/documentation/api`` page so analysts can pull
the curated API reference up without leaving the terminal. The same content
is also exposed as the MCP tool ``documentation_api`` (see
``app/api/mcp_http.py``) for agent / Claude Desktop access — three surfaces
in lockstep so a public endpoint is reachable everywhere it can be looked up.
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown

docs_app = typer.Typer(help="Read in-app documentation from the terminal.")


def _api_reference_path() -> Path:
    """Resolve docs/api-reference.md relative to the source tree.

    Same resolution as ``app.web.router.documentation_api`` — the curated
    Markdown lives at the repo root so it is browsable on the GitHub mirror
    and shipped inside the source tree (not installed as package data,
    which would pin it to wheel layout).
    """
    return Path(__file__).resolve().parent.parent.parent / "docs" / "api-reference.md"


@docs_app.command("api")
def docs_api() -> None:
    """Render the API reference guide in the terminal (mirrors ``/documentation/api``)."""
    md_path = _api_reference_path()
    try:
        md_text = md_path.read_text(encoding="utf-8")
    except OSError:
        typer.echo(
            f"docs/api-reference.md missing (looked at {md_path})", err=True
        )
        raise typer.Exit(code=1)
    Console().print(Markdown(md_text))
