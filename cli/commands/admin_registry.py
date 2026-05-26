"""``agnes admin registry`` — export / import / seed the table registry.

Three flows, all going through the local repository factory so they work
identically against DuckDB legacy and Postgres backends:

  - ``agnes admin registry export <path>`` writes the current registry
    to a JSON Lines file. One row per line, every column included
    (incl. v26 Keboola fields, ``registered_at`` timestamps, encoded
    ``primary_key`` / ``where_filters`` JSON blobs). Idempotent.

  - ``agnes admin registry import <path>`` reads a JSON Lines file and
    upserts every row into the local registry. Conflict key is ``id``,
    so re-running against an unchanged file is a no-op. Missing local
    rows are inserted; existing ones get every column overwritten from
    the file. Optional ``--purge`` deletes local rows not present in
    the file (use with care — this also wipes registry rows that were
    legitimately added locally since the export).

  - ``agnes admin registry seed-from <ssh-host>`` is a convenience
    wrapper that runs the export against a remote server over SSH,
    streams the JSON Lines back via stdout, and imports it locally in
    one shot. Used to mirror production state onto a fresh dev box
    without manual scp.

The data path: each row is a flat dict matching the SQLAlchemy
``TableRegistry`` model. ``upsert_raw`` on the repository handles type
normalisation (``primary_key`` accepts list or comma-string or JSON
string; ``where_filters`` likewise tolerates the wire forms). Two
instances on different backends therefore exchange the same JSONL file
losslessly even though their internal column types differ.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import typer


registry_app = typer.Typer(
    help=(
        "Export, import, or seed the table_registry. "
        "Use `export` + `import` to mirror state between two Agnes "
        "instances (or commit a snapshot to git for fresh installs)."
    )
)


def _normalise_for_json(value: Any) -> Any:
    """Coerce DB-native types to JSON-safe scalars.

    ``datetime`` → ISO-8601 string (registered_at). Bytes → utf-8 string
    (defensive; no current column uses bytes). Everything else passes
    through.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _row_to_json(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _normalise_for_json(v) for k, v in row.items()}


@registry_app.command("export")
def export(
    path: str = typer.Argument(
        ...,
        help=(
            "Destination JSON Lines path. Use ``-`` to write to stdout "
            "(useful when piping into ``registry import`` over SSH)."
        ),
    ),
    source_type: str = typer.Option(
        "",
        "--source-type",
        help="Filter to a single source_type (keboola | bigquery | jira | internal). Empty = all rows.",
    ),
):
    """Dump the local table_registry to a JSON Lines file."""
    from src.repositories import table_registry_repo

    repo = table_registry_repo()
    if source_type:
        rows = repo.list_by_source(source_type)
    else:
        rows = repo.list_all()

    out_stream = sys.stdout if path == "-" else open(path, "w", encoding="utf-8")
    try:
        for row in rows:
            out_stream.write(json.dumps(_row_to_json(row), ensure_ascii=False))
            out_stream.write("\n")
    finally:
        if out_stream is not sys.stdout:
            out_stream.close()

    if path != "-":
        typer.echo(f"Exported {len(rows)} row(s) to {path}", err=True)


@registry_app.command("import")
def import_(
    path: str = typer.Argument(
        ...,
        help=(
            "Source JSON Lines path. Use ``-`` to read from stdin. "
            "Each line is one registry row as JSON."
        ),
    ),
    purge: bool = typer.Option(
        False,
        "--purge",
        help=(
            "Delete local rows whose ``id`` is NOT present in the file. "
            "Off by default so importing a partial-source export "
            "(e.g. ``--source-type keboola``) preserves rows from other "
            "sources. Turn on when fully mirroring a single upstream."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Parse + count rows. Do not write to the local registry.",
    ),
):
    """Upsert rows from a JSON Lines file into the local table_registry.

    Conflict key is ``id``; existing rows have every column overwritten
    from the file. Use ``--purge`` to also delete local rows missing
    from the file (full-mirror semantic).
    """
    from src.repositories import table_registry_repo

    in_stream = sys.stdin if path == "-" else open(path, "r", encoding="utf-8")
    try:
        rows: list[Dict[str, Any]] = []
        for line_no, line in enumerate(in_stream, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as e:
                typer.echo(
                    f"Skipping line {line_no}: invalid JSON ({e})",
                    err=True,
                )
    finally:
        if in_stream is not sys.stdin:
            in_stream.close()

    if dry_run:
        typer.echo(
            f"[dry-run] {len(rows)} row(s) parsed; no writes performed.",
            err=True,
        )
        return

    repo = table_registry_repo()
    inserted_or_updated = 0
    failed = 0
    incoming_ids: set[str] = set()
    for row in rows:
        rid = row.get("id")
        if not rid:
            typer.echo(f"Skipping row without 'id': {row!r}", err=True)
            failed += 1
            continue
        try:
            repo.upsert_raw(row)
            inserted_or_updated += 1
            incoming_ids.add(rid)
        except Exception as e:
            typer.echo(f"Failed to upsert {rid}: {e}", err=True)
            failed += 1

    deleted = 0
    if purge:
        existing = repo.list_all()
        for r in existing:
            if r["id"] not in incoming_ids:
                try:
                    repo.unregister(r["id"])
                    deleted += 1
                except Exception as e:
                    typer.echo(f"Failed to delete {r['id']}: {e}", err=True)

    summary = f"Imported: {inserted_or_updated} upserted, {failed} failed"
    if purge:
        summary += f", {deleted} purged"
    typer.echo(summary)


@registry_app.command("seed-from")
def seed_from(
    host: str = typer.Argument(
        ...,
        help=(
            "SSH target running another Agnes (e.g. ``data-analyst`` "
            "alias or ``user@10.0.0.5``). Must have ``agnes`` on PATH "
            "and an admin-scoped token in the default config dir."
        ),
    ),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Pass through to ``import --purge``. See its help.",
    ),
    source_type: str = typer.Option(
        "",
        "--source-type",
        help="Filter the remote export to one source_type.",
    ),
    remote_agnes: str = typer.Option(
        "agnes",
        "--remote-agnes",
        help="Path to the ``agnes`` binary on the remote side (e.g. ``/opt/agnes/bin/agnes``).",
    ),
):
    """Pipe a remote ``agnes admin registry export -`` into ``import -``.

    Hands operators a single command for the common bootstrap path:
    "I just stood up a fresh local instance, mirror production now."
    No intermediate file lands on disk.
    """
    remote_cmd = [remote_agnes, "admin", "registry", "export", "-"]
    if source_type:
        remote_cmd += ["--source-type", source_type]

    ssh_argv = ["ssh", host, " ".join(remote_cmd)]
    typer.echo(f"Running on remote: {' '.join(remote_cmd)}", err=True)
    proc = subprocess.Popen(ssh_argv, stdout=subprocess.PIPE)
    assert proc.stdout is not None

    from src.repositories import table_registry_repo
    repo = table_registry_repo()
    inserted_or_updated = 0
    failed = 0
    incoming_ids: set[str] = set()
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            typer.echo(f"Skipping line: invalid JSON ({e})", err=True)
            failed += 1
            continue
        rid = row.get("id")
        if not rid:
            typer.echo(f"Skipping row without 'id': {row!r}", err=True)
            failed += 1
            continue
        try:
            repo.upsert_raw(row)
            inserted_or_updated += 1
            incoming_ids.add(rid)
        except Exception as e:
            typer.echo(f"Failed to upsert {rid}: {e}", err=True)
            failed += 1

    proc.wait()
    if proc.returncode != 0:
        typer.echo(
            f"Remote export exited with status {proc.returncode}; "
            f"partial seed applied ({inserted_or_updated} upserted).",
            err=True,
        )
        raise typer.Exit(proc.returncode)

    deleted = 0
    if purge:
        existing = repo.list_all()
        for r in existing:
            if r["id"] not in incoming_ids:
                try:
                    repo.unregister(r["id"])
                    deleted += 1
                except Exception as e:
                    typer.echo(f"Failed to delete {r['id']}: {e}", err=True)

    summary = f"Seeded from {host}: {inserted_or_updated} upserted, {failed} failed"
    if purge:
        summary += f", {deleted} purged"
    typer.echo(summary)
