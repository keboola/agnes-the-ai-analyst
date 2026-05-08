"""`agnes admin news {show,draft,edit,publish,unpublish,versions,export}`.

Direct DB access (no API roundtrip), same convention as
`agnes admin metrics import` — agents and operators don't need a PAT
to author news content as long as they have local DB access.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

admin_news_app = typer.Typer(help="Admin: edit the /home + /news content")


def _connect():
    from src.db import get_system_db
    return get_system_db()


def _print_row(row: dict | None, label: str = "news") -> None:
    if row is None:
        typer.echo(f"({label}: none)")
        return
    typer.echo(f"version    : {row['version']}")
    typer.echo(f"status     : {'published' if row['published'] else 'draft'}")
    typer.echo(f"created_at : {row.get('created_at')}")
    typer.echo(f"updated_at : {row.get('updated_at')}")
    typer.echo(f"created_by : {row.get('created_by') or ''}")
    if row["published"]:
        typer.echo(f"pub_at     : {row.get('published_at')}")
        typer.echo(f"pub_by     : {row.get('published_by') or ''}")
    typer.echo("-- intro --")
    typer.echo(row.get("intro") or "")
    typer.echo("-- content --")
    typer.echo(row.get("content") or "")


def _load_from_file(path: str) -> tuple[str, str]:
    """Parse `--from FILE` (or `-` for stdin). YAML or JSON object with
    `intro` + `content` keys. Returns (intro, content)."""
    if path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception:  # pragma: no cover — fall back to JSON
        data = json.loads(text)
    if not isinstance(data, dict):
        raise typer.BadParameter("file must contain a mapping with `intro` and `content` keys")
    return str(data.get("intro") or ""), str(data.get("content") or "")


@admin_news_app.command("show")
def show(
    version: int = typer.Option(None, "--version", "-v", help="Show a specific version (default: current published)"),
):
    """Print the current published version, or a specific version."""
    from src.repositories.news_template import NewsTemplateRepository

    conn = _connect()
    try:
        repo = NewsTemplateRepository(conn)
        if version is not None:
            row = repo.get_version(version)
            if row is None:
                typer.echo(f"version {version} not found", err=True)
                raise typer.Exit(1)
        else:
            row = repo.get_current_published()
        _print_row(row, label="published")
    finally:
        conn.close()


@admin_news_app.command("draft")
def draft():
    """Print the active draft (or 'no draft' if none)."""
    from src.repositories.news_template import NewsTemplateRepository

    conn = _connect()
    try:
        row = NewsTemplateRepository(conn).get_active_draft()
        _print_row(row, label="draft")
    finally:
        conn.close()


@admin_news_app.command("edit")
def edit(
    intro: str = typer.Option(None, "--intro", help="Intro HTML (used on /home)"),
    content: str = typer.Option(None, "--content", help="Full content HTML (used on /news)"),
    from_file: str = typer.Option(None, "--from", help="Read {intro, content} from YAML/JSON file (`-` for stdin)"),
    by: str = typer.Option("admin@cli", "--by", help="Author email recorded on the row"),
):
    """Upsert the active draft. One of --from / (--intro and --content) is required."""
    from src.repositories.news_template import NewsTemplateRepository

    if from_file is not None:
        intro_v, content_v = _load_from_file(from_file)
    elif intro is None and content is None:
        typer.echo("error: provide --from FILE or --intro / --content", err=True)
        raise typer.Exit(2)
    else:
        intro_v = intro or ""
        content_v = content or ""

    conn = _connect()
    try:
        repo = NewsTemplateRepository(conn)
        row = repo.save_draft(intro=intro_v, content=content_v, by=by)
        typer.echo(f"saved draft v{row['version']} (by {by})")
    finally:
        conn.close()


@admin_news_app.command("publish")
def publish(
    by: str = typer.Option("admin@cli", "--by", help="Publisher email recorded on the row"),
):
    """Publish the active draft."""
    from src.repositories.news_template import NewsTemplateRepository, NoDraftError

    conn = _connect()
    try:
        try:
            row = NewsTemplateRepository(conn).publish_draft(by=by)
        except NoDraftError:
            typer.echo("no active draft to publish", err=True)
            raise typer.Exit(1)
        typer.echo(f"published v{row['version']} (by {by})")
    finally:
        conn.close()


@admin_news_app.command("unpublish")
def unpublish(
    version: int = typer.Argument(..., help="Version number to unpublish (becomes a draft)"),
    by: str = typer.Option("admin@cli", "--by", help="Operator email for the audit record"),
):
    """Flip a published version back to draft state."""
    from src.repositories.news_template import (
        AlreadyDraftError,
        NewsTemplateRepository,
        NotFoundError,
    )

    conn = _connect()
    try:
        try:
            row = NewsTemplateRepository(conn).unpublish(version=version, by=by)
        except NotFoundError:
            typer.echo(f"version {version} not found", err=True)
            raise typer.Exit(1)
        except AlreadyDraftError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(2)
        typer.echo(f"unpublished v{row['version']}")
    finally:
        conn.close()


@admin_news_app.command("versions")
def versions(limit: int = typer.Option(20, "--limit", help="Max rows to print")):
    """Table of versions: number, status, created_at, by, published_at."""
    from src.repositories.news_template import NewsTemplateRepository

    conn = _connect()
    try:
        rows = NewsTemplateRepository(conn).list_versions(limit=limit)
        if not rows:
            typer.echo("(no versions)")
            return
        typer.echo(f"{'v':>4}  {'status':<10}  {'created':<19}  {'by':<28}  {'published':<19}  intro")
        typer.echo("-" * 110)
        for r in rows:
            ca = r.get("created_at").strftime("%Y-%m-%d %H:%M:%S") if r.get("created_at") else ""
            pa = r.get("published_at").strftime("%Y-%m-%d %H:%M:%S") if r.get("published_at") else ""
            typer.echo(
                f"{r['version']:>4}  {r['status']:<10}  {ca:<19}  {(r.get('created_by') or ''):<28}  "
                f"{pa:<19}  {r.get('intro_preview') or ''}"
            )
    finally:
        conn.close()


@admin_news_app.command("export")
def export(path: str = typer.Argument(..., help="YAML file to write {intro, content} into")):
    """Dump the currently-published version to a YAML file (so the operator can edit + re-import)."""
    import yaml
    from src.repositories.news_template import NewsTemplateRepository

    conn = _connect()
    try:
        row = NewsTemplateRepository(conn).get_current_published()
        if row is None:
            typer.echo("no published version to export", err=True)
            raise typer.Exit(1)
        Path(path).write_text(
            yaml.safe_dump(
                {"intro": row.get("intro") or "", "content": row.get("content") or ""},
                allow_unicode=True,
                sort_keys=False,
                width=200,
            ),
            encoding="utf-8",
        )
        typer.echo(f"exported v{row['version']} to {path}")
    finally:
        conn.close()
