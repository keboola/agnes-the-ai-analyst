"""`agnes admin semantic-layer` — status of the Keboola semantic-layer import.

CLI counterpart to the semantic-layer API surface:

  - ``coverage`` → ``GET /api/admin/semantic-layer/coverage``

The number that matters is how many of the metrics a project publishes can
actually bind to a table registered here. Metrics for tables the instance does
not register are normal and are reported as a plain count — a semantic layer
usually describes far more of a project than any one instance registers.
"""

from __future__ import annotations

import json

import typer

from cli.client import api_get

admin_semantic_layer_app = typer.Typer(help="Admin: Keboola semantic-layer import status")

# Skip reasons, in the wording an operator can act on.
_REASON_LABELS = {
    "missing_name": "no name upstream",
    "embedded_sql_comment": "SQL comment in the expression swallows the FROM clause",
    "foreign_alias_reference": "references another dataset via an alias (needs a relationship)",
    "ambiguous_relationship": "more than one relationship touches this dataset",
    "unsupported_relationship_type": "relationship type is not supported",
    "unverified_relationship_direction": "dataset sits on the unverified side of the relationship",
}


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    msg = detail if isinstance(detail, str) else (resp.text or f"HTTP {resp.status_code}")
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


@admin_semantic_layer_app.command("coverage")
def coverage(
    as_json: bool = typer.Option(False, "--json"),
    limit: int = typer.Option(10, "--limit", help="Max unregistered tables / blocked metrics listed per project"),
):
    """Show how much of each connected project's semantic layer reaches Agnes."""
    resp = api_get("/api/admin/semantic-layer/coverage")
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return

    sources = data.get("sources") or []
    if not sources:
        typer.echo("No Keboola project has a master (owner) token configured.")
        typer.echo("Add one with: agnes admin connection secret --id <connection> --kind master")
        return

    for source in sources:
        project = source.get("project") or {}
        label = f"{source.get('name', '')}"
        if project.get("id") is not None:
            label += f"  (project {project['id']} {project.get('name', '')!r})"
        typer.echo(f"\n{label}")

        if source.get("error"):
            typer.echo(f"  error: {source['error']}")
            continue

        metrics = source.get("metrics") or {}
        upstream, importable = metrics.get("upstream", 0), metrics.get("importable", 0)
        typer.echo(f"  metrics:  {importable} / {upstream} upstream")
        typer.echo(f"  glossary: {(source.get('glossary') or {}).get('upstream', 0)} upstream")

        for warning in source.get("warnings") or []:
            typer.echo(f"  ! {warning.get('message', '')}")

        blocked = source.get("blocked") or []
        if blocked:
            typer.echo(f"  blocked by their own definition ({len(blocked)}):")
            for row in blocked[:limit]:
                reason = _REASON_LABELS.get(row.get("reason", ""), row.get("reason", ""))
                typer.echo(f"     {row.get('metric', '')} — {reason}")
            if len(blocked) > limit:
                typer.echo(f"     … and {len(blocked) - limit} more")

        unregistered = source.get("unregistered_tables") or []
        if unregistered:
            shown = ", ".join(unregistered[:limit])
            more = f" … and {len(unregistered) - limit} more" if len(unregistered) > limit else ""
            typer.echo(f"  no table registered here for {len(unregistered)} dataset(s): {shown}{more}")
            if importable == 0 and upstream:
                typer.echo("     register one with: agnes admin register-table --help")
