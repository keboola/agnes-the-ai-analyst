"""`agnes admin digest` — admin CRUD over maintained knowledge digests (K4, #799).

A maintained digest is an admin-defined markdown document (title + standing
instructions + a set of source Collections) that the scheduler regenerates
with an LLM only when the sources' content changes. Failures never wipe the
previous markdown — the digest is instead marked ``stale`` with a reason, and
that banner rides along to every analyst's laptop as part of
``.claude/rules/ka_<slug>.md`` (delivered by ``agnes pull``, gated by
``resource_grants`` on the ``knowledge_digest`` resource type).

CLI counterpart to the ``/api/admin/knowledge-digests`` surface. Each
subcommand maps 1:1 to one HTTP endpoint:

  - ``list``   → ``GET /api/admin/knowledge-digests``
  - ``show``   → ``GET /api/admin/knowledge-digests/{id}``
  - ``create`` → ``POST /api/admin/knowledge-digests``
  - ``edit``   → ``PUT /api/admin/knowledge-digests/{id}``
  - ``delete`` → ``DELETE /api/admin/knowledge-digests/{id}``

Destructive ops require ``--yes`` to skip the confirm prompt — same pattern
as ``agnes admin data-package delete``. ``<digest>`` arguments accept either
the id or the slug; the wrapper resolves slug→id via a list-and-match when
the supplied value doesn't already look like an id. The slug is immutable
after create (it becomes a filename on every analyst laptop), so ``edit``
has no ``--slug`` option.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get, api_post, api_delete, api_put

admin_digest_app = typer.Typer(help="Maintained digest CRUD (K4)")


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    msg = (
        detail
        if isinstance(detail, str)
        else (json.dumps(detail) if detail is not None else (resp.text or f"HTTP {resp.status_code}"))
    )
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


def _resolve_digest_id(digest_ref: str) -> str:
    """Accept either an id or a slug. Slugs are resolved via list+match.

    Round-trip cost (one extra GET) is fine — admins do this rarely and
    the alternative (a server lookup-by-slug endpoint) would only exist
    for the CLI's convenience.
    """
    resp = api_get(f"/api/admin/knowledge-digests/{digest_ref}")
    if resp.status_code == 200:
        return digest_ref
    # 404 → try resolving as slug
    resp = api_get("/api/admin/knowledge-digests")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json().get("items", [])
    for row in rows:
        if row.get("slug") == digest_ref or row.get("id") == digest_ref:
            return row["id"]
    typer.echo(f"Digest not found: {digest_ref}", err=True)
    raise typer.Exit(1)


@admin_digest_app.command("list")
def list_digests(as_json: bool = typer.Option(False, "--json")):
    """List all maintained digests (output_md truncated to a preview)."""
    resp = api_get("/api/admin/knowledge-digests")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json().get("items", [])
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"Maintained digests: {len(rows)}")
    if not rows:
        return
    slug_w = max(len("SLUG"), max(len(r.get("slug", "")) for r in rows))
    title_w = max(len("TITLE"), max(len(r.get("title", "")) for r in rows))
    typer.echo(f"{'ID':<14}  {'SLUG':<{slug_w}}  {'TITLE':<{title_w}}  {'STATUS':<8}  GENERATED_AT")
    for r in rows:
        status = r.get("status") or "pending"
        typer.echo(
            f"{r['id']:<14}  {r.get('slug', ''):<{slug_w}}  {r.get('title', ''):<{title_w}}  "
            f"{status:<8}  {r.get('generated_at') or ''}"
        )


@admin_digest_app.command("show")
def show_digest(
    digest_ref: str = typer.Argument(..., help="Digest id or slug"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Show one digest's full detail, including output_md and staleness."""
    digest_id = _resolve_digest_id(digest_ref)
    resp = api_get(f"/api/admin/knowledge-digests/{digest_id}")
    if resp.status_code != 200:
        _fail(resp)
    d = resp.json()
    if as_json:
        typer.echo(json.dumps(d, indent=2))
        return
    typer.echo(f"id:           {d.get('id')}")
    typer.echo(f"slug:         {d.get('slug')}")
    typer.echo(f"title:        {d.get('title')}")
    typer.echo(f"sources:      {', '.join(d.get('source_corpus_ids') or []) or '(none)'}")
    typer.echo(f"model:        {d.get('model') or ''}")
    typer.echo(f"generated_at: {d.get('generated_at') or 'never'}")
    status = d.get("status") or "pending"
    # Staleness is a first-class display concern — never a silent, hidden
    # detail. Print it prominently, right after the status line.
    if status == "stale":
        reason = d.get("status_reason") or "(no reason recorded)"
        typer.echo(f"status:       STALE — {reason}")
    else:
        typer.echo(f"status:       {status}")
    typer.echo("")
    typer.echo("instructions:")
    typer.echo(d.get("instructions") or "")
    typer.echo("")
    typer.echo("output_md:")
    typer.echo(d.get("output_md") or "(not yet generated)")


@admin_digest_app.command("create")
def create_digest(
    slug: str = typer.Option(..., "--slug", help="URL-safe stable id (becomes ka_<slug>.md on analyst laptops)"),
    title: str = typer.Option(..., "--title", help="Display title"),
    instructions: Optional[str] = typer.Option(
        None, "--instructions", help="Standing instructions for the LLM regeneration pass"
    ),
    instructions_file: Optional[str] = typer.Option(
        None, "--instructions-file", help="Path to a file containing the instructions"
    ),
    source: list[str] = typer.Option([], "--source", help="Source collection id (repeatable)"),
):
    """Create a new maintained digest.

    Exactly one of ``--instructions`` / ``--instructions-file`` is required.
    """
    resolved_instructions = _resolve_instructions(instructions, instructions_file)
    payload = {
        "slug": slug,
        "title": title,
        "instructions": resolved_instructions,
        "source_corpus_ids": list(source),
    }
    resp = api_post("/api/admin/knowledge-digests", json=payload)
    if resp.status_code != 201:
        _fail(resp)
    body = resp.json()
    typer.echo(f"Created knowledge_digest id={body.get('id')} slug={slug}")


def _resolve_instructions(instructions: Optional[str], instructions_file: Optional[str]) -> str:
    if instructions and instructions_file:
        typer.echo("Error: pass only one of --instructions / --instructions-file.", err=True)
        raise typer.Exit(2)
    if instructions_file:
        path = Path(instructions_file)
        if not path.exists():
            typer.echo(f"Error: instructions file not found: {path}", err=True)
            raise typer.Exit(2)
        return path.read_text(encoding="utf-8").strip()
    if instructions:
        return instructions
    typer.echo("Error: one of --instructions / --instructions-file is required.", err=True)
    raise typer.Exit(2)


@admin_digest_app.command("edit")
def edit_digest(
    digest_ref: str = typer.Argument(..., help="Digest id or slug"),
    title: Optional[str] = typer.Option(None, "--title"),
    instructions: Optional[str] = typer.Option(None, "--instructions"),
    instructions_file: Optional[str] = typer.Option(None, "--instructions-file"),
    source: list[str] = typer.Option(
        [], "--source", help="Source collection id (repeatable) — replaces the full list when passed"
    ),
):
    """Patch digest metadata. Only provided fields are updated. Slug is immutable."""
    payload: dict = {}
    if title is not None:
        payload["title"] = title
    if instructions_file is not None:
        payload["instructions"] = _resolve_instructions(None, instructions_file)
    elif instructions is not None:
        payload["instructions"] = instructions
    if source:
        payload["source_corpus_ids"] = list(source)
    if not payload:
        # Short-circuit BEFORE the slug-resolution roundtrip so callers
        # who pass nothing don't pay for a useless GET.
        typer.echo(
            "Nothing to update. Pass at least one of --title/--instructions/--instructions-file/--source.",
            err=True,
        )
        raise typer.Exit(2)
    digest_id = _resolve_digest_id(digest_ref)
    resp = api_put(f"/api/admin/knowledge-digests/{digest_id}", json=payload)
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Updated knowledge_digest {digest_id}")


@admin_digest_app.command("delete")
def delete_digest(
    digest_ref: str = typer.Argument(..., help="Digest id or slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete a maintained digest (and any dangling resource grants)."""
    digest_id = _resolve_digest_id(digest_ref)
    if not yes:
        confirm = typer.confirm(f"Delete knowledge_digest {digest_id}?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/admin/knowledge-digests/{digest_id}")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Deleted knowledge_digest {digest_id}")
