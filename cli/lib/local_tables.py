"""What counts as a locally-present table, in one place.

`agnes status` and `agnes diagnose` both have to answer "which tables does
this workspace actually hold?" and they must not drift: `diagnose` warns when
data looks missing, `status` prints the count, and an analyst comparing the
two would rightly treat a disagreement as a bug in one of them. The rule
lived only in `cli/commands/diagnose.py`; `status` grew a second copy, which
is what this module removes.

Two trees hold parquets, and only ONE of them is locally queryable:

- ``<workspace>/server/parquet/`` — the legacy flat flow. The DuckDB view
  rebuild (`cli/lib/pull.py::_rebuild_duckdb_views`) walks exactly this
  directory, so a table here is what `agnes query --local` can resolve.
- ``<workspace>/.claude/data/_shared/`` — the canonical store written by
  the per-type stack sync, i.e. step 8 of ``agnes pull`` (called the
  ``v49 unified stack`` in older code comments). Nothing in the tree
  registers views over it, so a table present only here is on disk but NOT
  locally queryable — verified against a live workspace: 37 parquets in
  `_shared`, zero corresponding views, and `agnes query --local` on one of
  them failing with a DuckDB catalog error while the default server-side
  scope answered fine.

Keep the two counts distinct rather than summing them — a single number
either hides real bytes on disk or promises data `--local` cannot reach.
"""

from __future__ import annotations

from pathlib import Path

_SHARED_SUBPATH = (".claude", "data", "_shared")


def local_table_names(parquet_dir: Path) -> set[str]:
    """Which tables are actually readable from `<workspace>/server/parquet/`.

    Mirrors the DuckDB view rebuild in `cli/lib/pull.py` exactly, because that
    rebuild is what decides whether `agnes query <table>` resolves — which is
    the question this check is really asking. A partitioned table lives as a
    DIRECTORY of parts (`server/parquet/<table_id>/**/*.parquet`) and gets ONE
    view named after the directory, so a top-level `*.parquet` glob misses it
    entirely and would warn about missing data the analyst already has.
    `.staging-<tid>` dirs are the debris of an interrupted partitioned sync and
    are never exposed as a view, so they are not a table either.
    """
    if not parquet_dir.exists():
        return set()
    names: set[str] = set()
    try:
        entries = list(parquet_dir.iterdir())
    except OSError:
        return set()
    for entry in entries:
        if entry.name.startswith(".staging-"):
            continue
        if entry.is_dir():
            if any(entry.rglob("*.parquet")):
                names.add(entry.name)
        elif entry.suffix == ".parquet":
            names.add(entry.stem)
    return names


def table_key(stem: str) -> str:
    """Normalize a parquet stem so one table matches across the two trees.

    The trees are keyed differently: `server/parquet/<stem>` uses the flat
    manifest key (`sync_state.table_id`, which is `table_registry.name` by
    convention — see `app/api/admin.py`), while `_shared/<stem>` uses
    `table_registry.id` (`cli/lib/pull_sync.py`). Registration derives the id
    from the name by lowercasing and turning spaces into underscores
    (`app/api/admin.py`), so this mirrors that derivation.

    It is a heuristic, not the authoritative mapping: Keboola auto-discovery
    builds the id from the *fully-qualified* source id (bucket included), so
    `id != slug(name)` there. That is why the two counts below are reported
    separately instead of being summed — an unmatched `_shared` stem is
    reported as "no local view" rather than silently merged or double-counted.
    """
    return stem.strip().lower().replace(" ", "_")


def shared_store_stems(workspace: Path) -> set[str]:
    """Table stems in the stack sync's canonical store, `.claude/data/_shared/`.

    Only `_shared/` is read: its `_direct/` and `<package_slug>/` siblings are
    reference links back into it (`cli/lib/pull_sync.py`), so reading those too
    would count one table once per package that ships it.
    """
    shared = workspace.joinpath(*_SHARED_SUBPATH)
    if not shared.is_dir():
        return set()
    return {p.stem for p in shared.glob("*.parquet")}


def count_local_tables(workspace: Path) -> tuple[int, int]:
    """``(queryable, downloaded_without_view)`` for this workspace.

    ``queryable`` counts tables the DuckDB view rebuild exposes, so it is what
    `agnes query --local` can resolve. ``downloaded_without_view`` counts
    tables sitting in the stack-sync store with no counterpart in the
    queryable set — real bytes on disk that `--local` cannot reach today.
    """
    queryable = local_table_names(workspace / "server" / "parquet")
    queryable_keys = {table_key(n) for n in queryable}
    unregistered = {s for s in shared_store_stems(workspace) if table_key(s) not in queryable_keys}
    return len(queryable), len(unregistered)
