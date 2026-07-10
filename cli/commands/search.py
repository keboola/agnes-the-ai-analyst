"""`agnes search` — unified knowledge search (K2, #797).

One query fans out server-side over document Collections (hybrid
lexical+vector), the corporate-memory knowledge base (fulltext), and the
table catalog (lexical cards). Table hits carry a pivot hint — query them
with SQL via `agnes query` instead of reading text.
"""

from __future__ import annotations

import json as json_lib

import typer

from cli.v2_client import V2ClientError, api_get_json

search_app = typer.Typer(help="Unified search across documents, knowledge, and the catalog.")


@search_app.callback(invoke_without_command=True)
def search(
    query: str = typer.Argument(..., help="Search query"),
    k: int = typer.Option(10, "--k", help="Max results"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """One query across documents, the knowledge base, and the data catalog."""
    try:
        body = api_get_json("/api/knowledge/search", q=query, k=k)
    except V2ClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return
    results = body.get("results", [])
    if not results:
        typer.echo("No matches.")
        return
    for r in results:
        t = r.get("type")
        if t == "chunk":
            typer.echo(
                f"[{r.get('score')}] doc  {r.get('filename')} #{r.get('ordinal')}: {(r.get('text') or '')[:110]}"
            )
        elif t == "knowledge":
            typer.echo(f"[{r.get('score')}] know {r.get('title')}: {(r.get('snippet') or '')[:110]}")
        else:
            typer.echo(f"[{r.get('score')}] tbl  {r.get('name')} — {r.get('pivot_hint')}")
