"""`agnes admin autodoc-tables` — LLM-generate descriptions for tables that
have none (#399).

Reads each undescribed registered table's stored profile (columns + sample
rows) and asks the configured LLM (Haiku by default) for a short factual
description, then saves it. Never overwrites a description that already exists.
The pure prompt/parse logic lives in :mod:`src.table_autodoc`; this command
just wires the repositories + extractor to it.
"""

from __future__ import annotations

from typing import Optional

import typer


def _build_extractor():
    """Construct the configured StructuredExtractor.

    Reads the instance ``ai:`` block via the overlay-aware
    ``app.instance_config.load_instance_config`` (so an ``ai:`` block written
    to ``${DATA_DIR}/state/instance.yaml`` is honoured), and falls back to
    ``ANTHROPIC_API_KEY`` / ``LLM_API_KEY`` from the environment. Raises
    ``ValueError`` with an actionable message when no LLM is configured.

    Mirrors the resolution in ``services/corporate_memory/collector.py`` and
    ``services/session_processors/verification.py``. Isolated as a module-level
    seam so tests can substitute a fake extractor.
    """
    from connectors.llm import create_extractor_from_env_or_config

    ai_config = None
    try:
        from app.instance_config import load_instance_config

        try:
            instance_config = load_instance_config()
        except (ValueError, FileNotFoundError):
            instance_config = {}
        ai_config = instance_config.get("ai") if instance_config else None
    except Exception:
        ai_config = None

    return create_extractor_from_env_or_config(ai_config)


def autodoc_tables(
    table: Optional[str] = typer.Option(
        None, "--table", help="Only document this one table id."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the generated descriptions; save nothing."
    ),
    limit: int = typer.Option(
        0, "--limit", min=0, help="Max tables to process (0 = no limit)."
    ),
):
    """Generate descriptions for undescribed tables from their sample data.

    Only tables whose ``description`` is empty are touched, and only those that
    have already been profiled (so sample rows exist). Re-run any time; an
    existing description is never clobbered.
    """
    from connectors.llm.exceptions import LLMError
    from src.db import get_system_db
    from src.repositories.profiles import ProfileRepository
    from src.repositories.table_registry import TableRegistryRepository
    from src.table_autodoc import generate_description

    conn = get_system_db()
    try:
        reg = TableRegistryRepository(conn)
        profiles = ProfileRepository(conn)

        rows = reg.list_all()
        if table:
            rows = [r for r in rows if r.get("id") == table]
            if not rows:
                typer.echo(f"No registered table with id {table!r}.", err=True)
                raise typer.Exit(1)

        targets = [r for r in rows if not (r.get("description") or "").strip()]
        if not targets:
            typer.echo("All matching tables already have descriptions. Nothing to do.")
            return
        if limit:
            targets = targets[:limit]

        try:
            extractor = _build_extractor()
        except ValueError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)

        described = 0
        skipped = 0
        for r in targets:
            tid = r["id"]
            profile = profiles.get(tid)
            if not profile or not (profile.get("sample_rows") or profile.get("columns")):
                typer.echo(
                    f"  skip {tid}: no profile/sample data yet (sync + profile first)",
                    err=True,
                )
                skipped += 1
                continue
            try:
                desc = generate_description(
                    extractor,
                    r.get("name") or tid,
                    profile.get("columns"),
                    profile.get("sample_rows"),
                    source=r.get("source_type"),
                )
            except LLMError as e:
                typer.echo(f"  skip {tid}: LLM error: {e}", err=True)
                skipped += 1
                continue
            if not desc:
                typer.echo(f"  skip {tid}: model returned an empty description", err=True)
                skipped += 1
                continue

            if dry_run:
                typer.echo(f"\n[{tid}] (dry-run, not saved)\n  {desc}")
            else:
                reg.set_description(tid, desc)
                typer.echo(f"  ✓ {tid}: {desc}")
            described += 1

        verb = "would describe" if dry_run else "described"
        typer.echo(f"\n{verb} {described} table(s); skipped {skipped}.")
    finally:
        conn.close()
