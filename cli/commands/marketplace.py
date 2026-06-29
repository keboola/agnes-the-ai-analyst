"""`agnes marketplace {search,detail,add,remove}` — unified marketplace CLI.

Replaces the legacy `agnes my-stack toggle` (curated only, opt-out era) and
the consumer-facing `agnes store install/uninstall/list/show`. Both Curated
and Flea Market items are handled through a single command surface that mirrors
the current web marketplace.

ID format:
  Curated → marketplace_id/plugin_name  (contains a slash)
  Flea    → UUID without slash
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from cli.v2_client import V2ClientError, api_delete, api_get_json, api_post_json

marketplace_app = typer.Typer(help="Browse and manage your Agnes marketplace stack")


def _parse_id(item_id: str) -> tuple[str, str, str]:
    """Return (source, part1, part2).

    Curated: "/" in ID → ("curated", marketplace_id, plugin_name)
    Flea:    no slash  → ("flea", entity_id, "")
    """
    if "/" in item_id:
        parts = item_id.split("/", 1)
        return "curated", parts[0], parts[1]
    return "flea", item_id, ""


@marketplace_app.command("search")
def search(
    query: Optional[str] = typer.Option(None, "-q", "--query", help="Search text"),
    type: Optional[str] = typer.Option(None, "--type", help="skill | agent | plugin"),
    source: Optional[str] = typer.Option(None, "--source", help="curated | flea (default: both)"),
    sort: str = typer.Option("recent", "--sort", help="recent | most_used | trending"),
    limit: int = typer.Option(24, "--limit", min=1, max=100),
    json_out: bool = typer.Option(False, "--json"),
):
    """Search Curated and Flea Market; returns only items you have access to."""
    tabs = [source] if source else ["curated", "flea"]
    all_items: list = []
    for tab in tabs:
        params: dict = {"tab": tab, "sort": sort, "page_size": limit}
        if query:
            params["q"] = query
        if type:
            params["type"] = type
        try:
            body = api_get_json("/api/marketplace/items", **params)
        except V2ClientError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1)
        all_items.extend(body.get("items", []))

    if json_out:
        typer.echo(json.dumps({"items": all_items, "total": len(all_items)}, indent=2))
        return

    if not all_items:
        typer.echo("No results.")
        return

    label = f'"{query}"' if query else "marketplace"
    typer.echo(f"{len(all_items)} result(s) for {label}:")
    for it in all_items:
        status = "✓ in stack" if it.get("installed") else "+ add"
        typer.echo(
            f"  [{it.get('type', '?'):6s}] [{it.get('source', '?'):7s}] "
            f"{it.get('name', '?'):30s} by {it.get('owner', '?'):20s} "
            f"{status:10s}  id={it['id']}"
        )


@marketplace_app.command("detail")
def detail(
    item_id: str = typer.Argument(..., help="Item ID: marketplace_id/plugin_name or UUID"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Show full details for a marketplace item (curated or flea)."""
    source, part1, part2 = _parse_id(item_id)
    try:
        if source == "curated":
            body = api_get_json(f"/api/marketplace/curated/{part1}/{part2}")
        else:
            body = api_get_json(f"/api/marketplace/flea/{part1}/detail")
    except V2ClientError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    if json_out:
        typer.echo(json.dumps(body, indent=2))
        return

    name = body.get("display_name") or body.get("plugin_name") or body.get("manifest_name") or "?"
    item_type = body.get("type", "plugin")
    version = body.get("version") or "?"
    src_label = f"curated: {body.get('marketplace_id')}" if source == "curated" else "flea"
    installed = body.get("installed", False)

    typer.echo(f"{name} ({item_type}) v{version}  [{src_label}]")
    typer.echo(f"  {'✓ In your stack' if installed else '+ Not in stack'}")

    if body.get("tagline"):
        typer.echo(f"\n  {body['tagline']}")
    if body.get("description"):
        typer.echo(f"\n  {body['description']}")

    use_cases = body.get("use_cases", [])
    if use_cases:
        typer.echo("\n  Use cases:")
        for uc in use_cases:
            title = uc.get("title") or uc if isinstance(uc, str) else str(uc)
            typer.echo(f"    • {title}")

    skills = body.get("skills", [])
    agents = body.get("agents", [])
    commands = body.get("commands", [])
    mcps = body.get("mcps", [])

    if any([skills, agents, commands, mcps]):
        typer.echo("\n  Contents:")
        if skills:
            typer.echo(f"    Skills:      {', '.join(s.get('name', '?') for s in skills)}")
        if agents:
            typer.echo(f"    Agents:      {', '.join(a.get('name', '?') for a in agents)}")
        if commands:
            names = [c if isinstance(c, str) else c.get("name", "?") for c in commands]
            typer.echo(f"    Commands:    {', '.join(names)}")
        if mcps:
            names = [m if isinstance(m, str) else m.get("name", "?") for m in mcps]
            typer.echo(f"    MCP servers: {', '.join(names)}")

    if not installed:
        typer.echo(f"\n  Add to stack: agnes marketplace add {item_id}")


@marketplace_app.command("add")
def add(
    item_id: str = typer.Argument(..., help="Item ID: marketplace_id/plugin_name or UUID"),
):
    """Add a plugin, skill, or agent to your stack."""
    source, part1, part2 = _parse_id(item_id)
    try:
        if source == "curated":
            api_post_json(f"/api/marketplace/curated/{part1}/{part2}/install", {})
        else:
            api_post_json(f"/api/store/entities/{part1}/install", {})
    except V2ClientError as e:
        _handle_install_error(e)
        raise typer.Exit(1)
    typer.echo("Added to your stack. Run /update-agnes-plugins in Claude Code to activate.")


@marketplace_app.command("remove")
def remove(
    item_id: str = typer.Argument(..., help="Item ID: marketplace_id/plugin_name or UUID"),
):
    """Remove a plugin, skill, or agent from your stack."""
    source, part1, part2 = _parse_id(item_id)
    try:
        if source == "curated":
            api_delete(f"/api/marketplace/curated/{part1}/{part2}/install")
        else:
            api_delete(f"/api/store/entities/{part1}/install")
    except V2ClientError as e:
        _handle_install_error(e)
        raise typer.Exit(1)
    typer.echo("Removed from your stack. Run /update-agnes-plugins in Claude Code to apply.")


def _handle_install_error(e: V2ClientError) -> None:
    if e.status_code == 409:
        body = e.body if isinstance(e.body, dict) else {}
        detail_str = body.get("detail", "")
        if "system" in detail_str:
            typer.echo("Cannot modify — this is a system plugin managed by your admin.", err=True)
        elif "approved" in detail_str:
            typer.echo("This item is not yet approved and cannot be installed.", err=True)
        else:
            typer.echo(str(e), err=True)
    elif e.status_code == 403:
        typer.echo("You do not have permission to access this plugin.", err=True)
    else:
        typer.echo(str(e), err=True)


# --------------------------------------------------------------------------- #
# Curator tool — scaffold the Agnes enrichment file (Gap 2 / #469).
#
# Purely local: operates on a cloned curated-marketplace repo, no server call.
# --------------------------------------------------------------------------- #


@marketplace_app.command("scaffold-metadata")
def scaffold_metadata(
    path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        help="Path to a cloned curated-marketplace repo "
        "(contains .claude-plugin/marketplace.json).",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Exit non-zero if the on-disk file is out of sync. Writes nothing. "
        "Intended for CI.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the would-be file; write nothing."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="With --dry-run, print only the JSON (no report)."
    ),
):
    """Generate / refresh .claude-plugin/marketplace-metadata.json (curator tool).

    Derives display_name, tagline, invocation, and when_to_use from
    marketplace.json + each plugin.json + SKILL.md / agent frontmatter, while
    preserving every human-authored field (cover photos, polished copy, sample
    interactions). Re-run any time the plugins change; human edits always win.
    """
    from src.marketplace_metadata import (
        MARKETPLACE_METADATA_REL,
        read_marketplace_metadata,
    )
    from src.marketplace_metadata_scaffold import (
        ScaffoldError,
        comparable_view,
        render_document,
        scaffold_metadata as _scaffold,
    )

    try:
        existing = read_marketplace_metadata(path)
        doc, report = _scaffold(path, existing=existing)
    except ScaffoldError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    if check:
        if comparable_view(existing) != comparable_view(doc):
            typer.echo(
                "marketplace-metadata.json is OUT OF SYNC — run: "
                f"agnes marketplace scaffold-metadata {path}",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo("marketplace-metadata.json is up to date.")
        return

    rendered = render_document(doc)

    if dry_run:
        if json_out:
            typer.echo(rendered)
            return
        _print_scaffold_report(report)
        typer.echo("\n--- marketplace-metadata.json (dry-run, not written) ---")
        typer.echo(rendered)
        return

    target = path / MARKETPLACE_METADATA_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    _print_scaffold_report(report)
    typer.echo(f"\nWrote {target}")


def _print_scaffold_report(report) -> None:
    typer.echo(
        f"Plugins: {len(report.plugins)}  "
        f"Skills: {len(report.skills)}  Agents: {len(report.agents)}"
    )
    counts = report.status_counts()
    if counts:
        typer.echo(
            "Fields: " + ", ".join(f"{k}={counts[k]}" for k in sorted(counts))
        )
    for w in report.warnings:
        typer.echo(f"  warning: {w}", err=True)
    if report.orphans:
        typer.echo(
            f"  note: {len(report.orphans)} section(s) in the file are not in "
            "marketplace.json (kept untouched): " + ", ".join(report.orphans)
        )
