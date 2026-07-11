"""`agnes admin data-semantics generate` — scaffold the workspace
data-semantics pack from the catalog (Gap 1 / #469).

Reads ``data_packages`` + ``table_registry`` (+ ``column_metadata`` and
``bq_metadata_cache``) and ``metric_definitions`` from the active state
backend (via the repository factory) and emits a *starter* pack
(``<slug>/tables/*.yml``, ``<slug>/metrics/*.yml``, ``_brief.md``,
``_overview.md``) into an output directory. Re-run any time the
catalog changes — human-authored content always wins (see
:mod:`src.data_semantics_scaffold`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import typer

admin_data_semantics_app = typer.Typer(
    help="Generate the workspace data-semantics pack from the catalog"
)


@admin_data_semantics_app.callback()
def _data_semantics_main() -> None:
    """Generate the workspace data-semantics pack from the catalog (#469).

    A no-op group callback: it keeps ``generate`` an explicit subcommand
    (``agnes admin data-semantics generate ...``) rather than letting Typer
    hoist the single command and drop the verb.
    """


@admin_data_semantics_app.command("generate")
def generate(
    output_dir: Path = typer.Argument(
        ...,
        file_okay=False,
        dir_okay=True,
        help="Directory to emit the pack into (the workspace 'data/' root).",
    ),
    package: Optional[str] = typer.Option(
        None, "--package", help="Only generate this data-package slug."
    ),
    check: bool = typer.Option(
        False, "--check",
        help="Exit non-zero if the on-disk pack is out of sync. Writes nothing (CI).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the would-be files; write nothing."
    ),
    json_out: bool = typer.Option(
        False, "--json", help="With --dry-run, print {relpath: content} as JSON."
    ),
):
    """Generate / refresh the workspace data-semantics pack (admin tool).

    Derives table specs (columns, partition/cluster keys) and metric
    definitions from the catalog, leaving hand-authored know-how (gotchas,
    join contracts, query recipes) for a human. ``_brief.md`` / ``_overview.md``
    are seeded only when absent.
    """
    from src.data_semantics_scaffold import (
        ScaffoldError,
        comparable_view,
        scaffold_pack,
    )

    inputs, notes = _assemble_inputs(package_filter=package)

    if not inputs["packages"]:
        typer.echo(
            "No data packages found"
            + (f" for slug {package!r}." if package else " in the catalog."),
            err=True,
        )
        raise typer.Exit(1)

    existing = _read_existing(output_dir, inputs)

    try:
        files, report = scaffold_pack(inputs, existing=existing)
    except ScaffoldError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    for n in notes:
        report.warnings.append(n)

    if check:
        on_disk = _read_rendered(output_dir, files.keys())
        if comparable_view(on_disk) != comparable_view(files):
            typer.echo(
                "data-semantics pack is OUT OF SYNC — run: "
                f"agnes admin data-semantics generate {output_dir}",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo("data-semantics pack is up to date.")
        return

    if dry_run:
        if json_out:
            import json as _json

            typer.echo(_json.dumps(files, indent=2, ensure_ascii=False))
            return
        _print_report(report)
        for rel in sorted(files):
            typer.echo(f"\n--- {rel} (dry-run, not written) ---")
            typer.echo(files[rel])
        return

    for rel, text in files.items():
        target = output_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _print_report(report)
    typer.echo(f"\nWrote {len(files)} file(s) under {output_dir}")


# --------------------------------------------------------------------------- #
# Assembly — repositories -> the engine's pre-grouped plain-data input
# --------------------------------------------------------------------------- #


def _assemble_inputs(*, package_filter: Optional[str] = None):
    """Build ``{"packages": [...]}`` for :func:`scaffold_pack`.

    Reads through the backend-aware repository factory so the pack is
    generated from the live catalog on either state backend (DuckDB or
    Postgres) — never from the always-DuckDB connection (#513/#518).

    Returns ``(inputs, notes)`` where ``notes`` are CLI-level warnings (e.g.
    metrics that belong to no package and were dropped)."""
    from src.repositories import (
        bq_metadata_cache_repo,
        column_metadata_repo,
        data_packages_repo,
        metric_repo,
        table_registry_repo,
    )

    pkg_repo = data_packages_repo()
    tbl_repo = table_registry_repo()
    col_repo = column_metadata_repo()
    bq_repo = bq_metadata_cache_repo()
    met_repo = metric_repo()

    members = pkg_repo.list_member_ids_bulk()  # {pkg_id: [table_id, ...]}
    all_metrics = met_repo.list()

    packages_out: List[Dict[str, Any]] = []
    assigned_metric_ids: set = set()

    for pkg in pkg_repo.list():
        slug = pkg.get("slug")
        if package_filter and slug != package_filter:
            continue
        table_ids = list(members.get(pkg.get("id"), []))
        tid_set = set(table_ids)

        tables: List[Dict[str, Any]] = []
        for tid in table_ids:
            row = tbl_repo.get(tid)
            if not row:
                continue
            row = dict(row)
            row["columns"] = col_repo.list_for_table(tid)
            row["bq_cache"] = bq_repo.get(tid)
            tables.append(row)

        metrics: List[Dict[str, Any]] = []
        for mr in all_metrics:
            if tid_set & set(_metric_tables(mr)):
                metrics.append(mr)
                assigned_metric_ids.add(mr.get("id"))

        packages_out.append({
            "slug": slug,
            "name": pkg.get("name"),
            "description": pkg.get("description"),
            "tables": tables,
            "metrics": metrics,
        })

    notes: List[str] = []
    if not package_filter:
        unassigned = [m for m in all_metrics if m.get("id") not in assigned_metric_ids]
        if unassigned:
            sample = ", ".join(sorted(m.get("name", "?") for m in unassigned)[:5])
            notes.append(
                f"{len(unassigned)} metric(s) belong to no data package and were "
                f"not emitted (e.g. {sample}). Add their tables to a package first."
            )
    return {"packages": packages_out}, notes


def _metric_tables(metric: Dict[str, Any]) -> List[str]:
    # Union of `tables` + `table_name`, de-duplicated — mirrors the engine's
    # coordinate derivation (src.data_semantics_scaffold._derive_metric) so
    # package-matching and the emitted coordinate stay consistent.
    tables = list(metric.get("tables") or [])
    tn = metric.get("table_name")
    if tn and tn not in tables:
        tables.append(tn)
    return tables


# --------------------------------------------------------------------------- #
# Disk IO
# --------------------------------------------------------------------------- #


def _read_existing(output_dir: Path, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Read + parse every on-disk pack file for the packages in scope, keyed by
    relpath (YAML → list/dict, markdown → str)."""
    import yaml

    existing: Dict[str, Any] = {}
    for pkg in inputs["packages"]:
        base = output_dir / (pkg.get("slug") or "")
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(output_dir).as_posix()
            if path.suffix in (".yml", ".yaml"):
                try:
                    existing[rel] = yaml.safe_load(path.read_text(encoding="utf-8"))
                except yaml.YAMLError:
                    existing[rel] = None
            elif path.suffix == ".md":
                existing[rel] = path.read_text(encoding="utf-8")
    return existing


def _read_rendered(output_dir: Path, relpaths: Iterable[str]) -> Dict[str, str]:
    """Raw text of each generated relpath that exists on disk (for --check)."""
    on_disk: Dict[str, str] = {}
    for rel in relpaths:
        p = output_dir / rel
        if p.is_file():
            on_disk[rel] = p.read_text(encoding="utf-8")
    return on_disk


def _print_report(report) -> None:
    typer.echo(
        f"Packages: {len(report.packages)}  "
        f"Tables: {len(report.tables)}  Metrics: {len(report.metrics)}"
    )
    counts = report.status_counts()
    if counts:
        typer.echo("Fields: " + ", ".join(f"{k}={counts[k]}" for k in sorted(counts)))
    if report.briefs_seeded:
        typer.echo(f"Seeded {len(report.briefs_seeded)} brief/overview file(s).")
    for w in report.warnings:
        typer.echo(f"  warning: {w}", err=True)
