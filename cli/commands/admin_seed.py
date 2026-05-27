"""``agnes admin seed`` — export / import the operator-curated state across
data tables, the curated marketplace, and the flea market.

The three entity classes the user cares about for cross-instance mirror:

  - **data tables** (``table_registry``) — registry of pull-mode and
    remote-mode tables, including v26 Keboola incremental fields.
  - **curated marketplace** (``marketplace_registry`` +
    ``marketplace_plugins``) — admin-registered upstream marketplaces and
    the plugin manifests their nightly clone produced.
  - **flea market** (``store_entities``) — user-uploaded skill / agent /
    command entities and their visibility status. Submission history
    (``store_submissions``) is excluded by default — it's audit-local
    and per-instance; include explicitly with ``--include store_submissions``
    if you really want it.

Wire format is a single JSON file with one key per table, mapping to a
list of row dicts::

    {
      "table_registry": [{...}, {...}],
      "marketplace_registry": [...],
      "marketplace_plugins": [...],
      "store_entities": [...]
    }

Re-imports are idempotent: ``ON CONFLICT (<pk>) DO UPDATE`` uses each
table's primary key (single-column for most, composite ``(marketplace_id,
name)`` for ``marketplace_plugins``). The ``--purge`` flag deletes local
rows whose PK isn't in the import — turn it on when you want an exact
mirror of the source instance.

This is the symmetric counterpart of ``agnes admin registry`` (kept as a
focused single-table command for back-compat). The seed group is the
preferred entrypoint for new operator workflows.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import typer


seed_app = typer.Typer(
    help=(
        "Export / import the operator-curated state across data tables, "
        "curated marketplace, and flea market. Used to mirror an Agnes "
        "instance onto a fresh dev box without scp'ing the database."
    )
)


# Each entity maps to: (DB table name, primary-key column tuple).
# Composite keys (e.g. ``marketplace_plugins``) use a multi-element
# tuple; ``ON CONFLICT`` builds against the full key.
SEED_TABLES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "table_registry":        ("table_registry",        ("id",)),
    "marketplace_registry":  ("marketplace_registry",  ("id",)),
    "marketplace_plugins":   ("marketplace_plugins",   ("marketplace_id", "name")),
    "store_entities":        ("store_entities",        ("id",)),
    # Submission history is per-instance audit detail. Off by default;
    # opt in explicitly when you need it.
    "store_submissions":     ("store_submissions",     ("id",)),
}

# Default include set when the operator doesn't pass --include. Mirrors
# "data tables + curated marketplace + flea market" from the seed group
# help. ``store_submissions`` is intentionally absent here.
DEFAULT_INCLUDES: Tuple[str, ...] = (
    "table_registry",
    "marketplace_registry",
    "marketplace_plugins",
    "store_entities",
)


def _parse_include(include: str) -> List[str]:
    if not include:
        return list(DEFAULT_INCLUDES)
    names = [n.strip() for n in include.split(",") if n.strip()]
    unknown = [n for n in names if n not in SEED_TABLES]
    if unknown:
        raise typer.BadParameter(
            f"unknown table(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(SEED_TABLES)}"
        )
    return names


def _json_safe(value: Any) -> Any:
    """Coerce DB-native types to JSON-safe scalars."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _row_to_json(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _json_safe(v) for k, v in row.items()}


def _column_names(engine, table: str) -> List[str]:
    import sqlalchemy as sa
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "ORDER BY ordinal_position"
            ),
            {"t": table},
        ).all()
    return [r[0] for r in rows]


def _column_types(engine, table: str) -> Dict[str, str]:
    """Map column → ``data_type`` (PG's ``json`` / ``jsonb`` need
    ``CAST(:param AS JSONB)`` so SA doesn't try to bind a Python dict as
    a bytes-typed text value).
    """
    import sqlalchemy as sa
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        ).all()
    return {r[0]: r[1] for r in rows}


def _dump_table(engine, table: str) -> List[Dict[str, Any]]:
    import sqlalchemy as sa
    with engine.connect() as conn:
        rows = conn.execute(sa.text(f"SELECT * FROM {table}")).mappings().all()
    return [_row_to_json(dict(r)) for r in rows]


def _upsert_row(
    engine,
    table: str,
    pk_cols: Sequence[str],
    cols: Sequence[str],
    col_types: Dict[str, str],
    row: Dict[str, Any],
) -> None:
    import sqlalchemy as sa

    present = [c for c in cols if c in row]
    if not present:
        return

    # JSON / JSONB columns: bind a Python str / dict and CAST so the
    # adapter doesn't fail with "can't adapt type" on dict values, and so
    # incoming JSON strings round-trip as structure rather than text.
    def _placeholder(col: str) -> str:
        dt = col_types.get(col, "")
        if dt in ("json", "jsonb"):
            return f"CAST(:{col} AS JSONB)"
        return f":{col}"

    cols_sql = ", ".join(present)
    placeholders = ", ".join(_placeholder(c) for c in present)
    pk_sql = ", ".join(pk_cols)
    update_cols = [c for c in present if c not in pk_cols]
    if update_cols:
        set_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        on_conflict = f"ON CONFLICT ({pk_sql}) DO UPDATE SET {set_sql}"
    else:
        # All-PK row (no non-PK columns to update) → conflict is a no-op.
        on_conflict = f"ON CONFLICT ({pk_sql}) DO NOTHING"

    sql = (
        f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders}) {on_conflict}"
    )

    # JSON values may arrive as Python dict/list (when read from another
    # in-process engine), as a JSON-encoded string (file we just wrote),
    # or as a plain string scalar that should be the JSON value (a
    # ``source_spec`` of ``"./plugins/X"`` round-tripped through
    # ``json.dumps`` decodes to the bare Python string, not to a JSON
    # quoted string — ``CAST(... AS JSONB)`` then sees an unquoted
    # ``./plugins/X`` and rejects it). Three rules:
    #
    #   - dict / list   → json.dumps unconditionally.
    #   - str that parses as JSON → pass through.
    #   - str that doesn't parse → wrap with json.dumps (treat as
    #     JSON scalar).
    params: Dict[str, Any] = {}
    for c in present:
        v = row.get(c)
        if col_types.get(c) in ("json", "jsonb") and v is not None:
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            elif isinstance(v, str):
                try:
                    json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    v = json.dumps(v)
            else:
                v = json.dumps(v)
        params[c] = v

    with engine.begin() as conn:
        conn.execute(sa.text(sql), params)


def _row_pk(row: Dict[str, Any], pk_cols: Sequence[str]) -> Tuple:
    return tuple(row.get(c) for c in pk_cols)


@seed_app.command("export")
def export(
    path: str = typer.Argument(
        ...,
        help=(
            "Destination JSON file. Use ``-`` to write to stdout (pipe "
            "to ``import -`` on the receiving side)."
        ),
    ),
    include: str = typer.Option(
        "",
        "--include",
        help=(
            "Comma-separated entity list. Default = "
            + ",".join(DEFAULT_INCLUDES)
            + ". Add ``store_submissions`` to include flea-market audit "
            "history (off by default — per-instance, not load-bearing)."
        ),
    ),
):
    """Dump the operator-curated state to a JSON file.

    Output shape::

        {
          "table_registry": [...],
          "marketplace_registry": [...],
          "marketplace_plugins": [...],
          "store_entities": [...]
        }

    Each list contains every row of the corresponding table with every
    column flattened to JSON-safe scalars (datetimes → ISO-8601 strings,
    bytes → UTF-8 strings).
    """
    from src.db_pg import get_engine

    names = _parse_include(include)
    engine = get_engine()

    bundle: Dict[str, List[Dict[str, Any]]] = {}
    counts: Dict[str, int] = {}
    for name in names:
        table, _ = SEED_TABLES[name]
        rows = _dump_table(engine, table)
        bundle[name] = rows
        counts[name] = len(rows)

    text = json.dumps(bundle, indent=2, ensure_ascii=False, sort_keys=True)

    if path == "-":
        sys.stdout.write(text + "\n")
    else:
        Path(path).write_text(text + "\n", encoding="utf-8")

    summary = ", ".join(f"{name}={counts[name]}" for name in names)
    typer.echo(f"Exported: {summary}", err=True)


@seed_app.command("import")
def import_(
    path: str = typer.Argument(
        ...,
        help=(
            "Source JSON file produced by ``seed export``. Use ``-`` to "
            "read from stdin."
        ),
    ),
    include: str = typer.Option(
        "",
        "--include",
        help=(
            "Restrict to a comma-separated subset of entity names. "
            "Default = whatever the file contains."
        ),
    ),
    purge: bool = typer.Option(
        False,
        "--purge",
        help=(
            "For each imported entity, HARD-DELETE local rows whose PK "
            "is not in the file. Off by default so a partial export "
            "(e.g. ``--include table_registry``) doesn't wipe the rest. "
            "Requires ``--yes`` for non-interactive confirmation."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=(
            "Skip the interactive confirmation prompt that ``--purge`` "
            "normally asks for. Required for non-TTY callers (CI / "
            "scripted bootstrap)."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Parse + count. Do not write or delete.",
    ),
):
    """Upsert the operator-curated state from a JSON file.

    Idempotent on the primary key of each table. With ``--purge``,
    also HARD-DELETEs local rows whose PK is absent from the file —
    i.e. exact mirror of the source instance, NOT a soft delete.
    Cascades from FKs (e.g. ``data_package_tables`` rows for a deleted
    ``data_packages.id``) are NOT undone; the DELETE relies on the
    DB's own ``ON DELETE`` rules.

    Reports three categories per table: ``inserted`` (PK was new),
    ``updated`` (PK matched an existing row; non-PK columns
    overwritten), ``failed`` (insert raised). With ``--purge``,
    ``deleted`` is also reported.
    """
    from src.db_pg import get_engine
    import sqlalchemy as sa

    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as e:
        typer.echo(f"Invalid JSON: {e}", err=True)
        raise typer.Exit(2)

    if not isinstance(bundle, dict):
        typer.echo("Bundle must be an object {table_name: [rows]}", err=True)
        raise typer.Exit(2)

    available = [n for n in bundle if n in SEED_TABLES]
    requested = _parse_include(include) if include else available
    names = [n for n in requested if n in available]

    if dry_run:
        for name in names:
            typer.echo(f"[dry-run] {name}: {len(bundle[name])} row(s)", err=True)
        return

    # ``--purge`` HARD-DELETES local rows. Gate behind an explicit
    # ``--yes`` so an operator who passed it by accident doesn't lose
    # state. Non-TTY callers (CI) must opt in via ``--yes``.
    if purge and not yes:
        if sys.stdin.isatty():
            typer.echo(
                "--purge HARD-DELETES local rows whose PK is missing from "
                "the file. This cannot be undone except from a backup.",
                err=True,
            )
            confirm = input("Type 'PURGE' to proceed: ").strip()
            if confirm != "PURGE":
                typer.echo("Aborted (no rows touched).", err=True)
                raise typer.Exit(1)
        else:
            typer.echo(
                "--purge requires --yes when stdin is not a TTY.",
                err=True,
            )
            raise typer.Exit(2)

    engine = get_engine()

    inserted = 0
    updated = 0
    failed = 0
    deleted = 0

    for name in names:
        table, pk_cols = SEED_TABLES[name]
        cols = _column_names(engine, table)
        col_types = _column_types(engine, table)
        rows = bundle.get(name, [])

        # Probe the existing PK set up front so we can classify each
        # incoming row as INSERT vs UPDATE vs CONFLICT. Without this,
        # an operator running ``import`` against a populated DB has no
        # way to tell whether they just overwrote 30 hand-tuned rows
        # or added 30 brand-new ones.
        with engine.connect() as conn:
            existing_rows = conn.execute(
                sa.text(f"SELECT {', '.join(pk_cols)} FROM {table}")
            ).all()
        existing_pks: set[Tuple] = {tuple(r) for r in existing_rows}

        incoming_pks: set[Tuple] = set()
        for row in rows:
            pk = _row_pk(row, pk_cols)
            if any(v is None for v in pk):
                typer.echo(f"Skipping {name} row with empty PK: {row!r}", err=True)
                failed += 1
                continue
            try:
                was_existing = pk in existing_pks
                _upsert_row(engine, table, pk_cols, cols, col_types, row)
                if was_existing:
                    updated += 1
                else:
                    inserted += 1
                incoming_pks.add(pk)
            except Exception as e:
                typer.echo(f"Failed to upsert {name} {pk}: {e}", err=True)
                failed += 1

        if purge:
            # Build a DELETE that targets rows whose PK is NOT in the
            # incoming set. Done in PK chunks to keep parameter counts
            # well below PG's 65535 limit on bind params.
            with engine.connect() as conn:
                existing = conn.execute(
                    sa.text(f"SELECT {', '.join(pk_cols)} FROM {table}")
                ).all()
            existing_pks = {tuple(r) for r in existing}
            to_delete = existing_pks - incoming_pks
            for pk in to_delete:
                where = " AND ".join(f"{c} = :pk_{i}" for i, c in enumerate(pk_cols))
                params = {f"pk_{i}": v for i, v in enumerate(pk)}
                try:
                    with engine.begin() as conn:
                        conn.execute(sa.text(f"DELETE FROM {table} WHERE {where}"), params)
                    deleted += 1
                except Exception as e:
                    typer.echo(f"Failed to delete {name} {pk}: {e}", err=True)

    summary = (
        f"Imported: {inserted} inserted, {updated} updated, {failed} failed"
        + (f", {deleted} purged" if purge else "")
    )
    typer.echo(summary)


@seed_app.command("seed-from")
def seed_from(
    host: str = typer.Argument(
        ...,
        help=(
            "SSH target running another Agnes (e.g. ``data-analyst`` "
            "alias or ``user@10.0.0.5``). Must have ``agnes`` on PATH "
            "and an admin-scoped token in the default config dir."
        ),
    ),
    include: str = typer.Option(
        "",
        "--include",
        help="Comma-separated entity list. Default matches `seed export`.",
    ),
    purge: bool = typer.Option(
        False,
        "--purge",
        help=(
            "Mirror semantics — HARD-DELETE local PKs missing from the "
            "remote. Requires ``--yes`` for non-interactive runs."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the ``--purge`` confirmation prompt (CI / scripted use).",
    ),
    remote_agnes: str = typer.Option(
        "agnes",
        "--remote-agnes",
        help="Path to ``agnes`` on the remote (e.g. ``/opt/agnes/bin/agnes``).",
    ),
):
    """Pipe ``agnes admin seed export -`` from a remote into local import.

    One command to bootstrap or refresh local state from another instance.
    Streams the JSON bundle over SSH stdin → stdout — no intermediate file.
    """
    import subprocess

    # Same purge gate as ``import``. The remote-stream path is even
    # easier to fire accidentally (one command instead of two), so the
    # ``--yes`` requirement matters more here.
    if purge and not yes:
        if sys.stdin.isatty():
            typer.echo(
                "--purge HARD-DELETES local rows whose PK is missing from the "
                "remote. Cannot be undone except from a backup.",
                err=True,
            )
            confirm = input("Type 'PURGE' to proceed: ").strip()
            if confirm != "PURGE":
                typer.echo("Aborted (no rows touched).", err=True)
                raise typer.Exit(1)
        else:
            typer.echo(
                "--purge requires --yes when stdin is not a TTY.",
                err=True,
            )
            raise typer.Exit(2)

    remote_cmd = [remote_agnes, "admin", "seed", "export", "-"]
    if include:
        remote_cmd += ["--include", include]

    ssh_argv = ["ssh", host, " ".join(remote_cmd)]
    typer.echo(f"Running on remote: {' '.join(remote_cmd)}", err=True)
    proc = subprocess.Popen(
        ssh_argv,
        stdout=subprocess.PIPE,
        stderr=sys.stderr.fileno(),
    )
    assert proc.stdout is not None

    raw = proc.stdout.read().decode("utf-8", errors="replace")
    rc = proc.wait()
    if rc != 0:
        typer.echo(f"Remote export exited with status {rc}", err=True)
        raise typer.Exit(rc)

    # Reuse the import path. Write to a temp-ish location so the import
    # function's error reporting (line numbers, etc.) works the same way
    # as the file-based flow.
    from src.db_pg import get_engine
    import sqlalchemy as sa

    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as e:
        typer.echo(f"Remote payload was not valid JSON: {e}", err=True)
        raise typer.Exit(2)
    if not isinstance(bundle, dict):
        typer.echo("Remote payload must be a {table_name: [rows]} object", err=True)
        raise typer.Exit(2)

    available = [n for n in bundle if n in SEED_TABLES]
    requested = _parse_include(include) if include else available
    names = [n for n in requested if n in available]

    engine = get_engine()
    inserted = 0
    updated = 0
    failed = 0
    deleted = 0

    for name in names:
        table, pk_cols = SEED_TABLES[name]
        cols = _column_names(engine, table)
        col_types = _column_types(engine, table)
        rows = bundle.get(name, [])
        with engine.connect() as conn:
            existing_rows = conn.execute(
                sa.text(f"SELECT {', '.join(pk_cols)} FROM {table}")
            ).all()
        existing_pks: set[Tuple] = {tuple(r) for r in existing_rows}
        incoming_pks: set[Tuple] = set()
        for row in rows:
            pk = _row_pk(row, pk_cols)
            if any(v is None for v in pk):
                typer.echo(f"Skipping {name} row with empty PK: {row!r}", err=True)
                failed += 1
                continue
            try:
                was_existing = pk in existing_pks
                _upsert_row(engine, table, pk_cols, cols, col_types, row)
                if was_existing:
                    updated += 1
                else:
                    inserted += 1
                incoming_pks.add(pk)
            except Exception as e:
                typer.echo(f"Failed to upsert {name} {pk}: {e}", err=True)
                failed += 1
        if purge:
            for pk in (existing_pks - incoming_pks):
                where = " AND ".join(f"{c} = :pk_{i}" for i, c in enumerate(pk_cols))
                params = {f"pk_{i}": v for i, v in enumerate(pk)}
                try:
                    with engine.begin() as conn:
                        conn.execute(sa.text(f"DELETE FROM {table} WHERE {where}"), params)
                    deleted += 1
                except Exception as e:
                    typer.echo(f"Failed to delete {name} {pk}: {e}", err=True)

    summary = (
        f"Seeded from {host}: {inserted} inserted, {updated} updated, {failed} failed"
        + (f", {deleted} purged" if purge else "")
    )
    typer.echo(summary)
