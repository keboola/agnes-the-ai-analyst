"""What counts as a locally-present table, in one place.

`agnes status` and `agnes diagnose` both have to answer "which tables does
this workspace actually hold?" and they must not drift: `diagnose` warns when
data looks missing, `status` prints the count, and an analyst comparing the
two would rightly treat a disagreement as a bug in one of them. The rule
lived only in `cli/commands/diagnose.py`; `status` grew a second copy, which
is what this module removes.

Parquets live in three places, and one of them is never directly queryable:

- ``<workspace>/server/parquet/`` — the legacy flat flow. The DuckDB view
  rebuild (`cli/lib/pull.py::_rebuild_duckdb_views`) walks this directory
  FIRST, so a table here is what `agnes query --local` resolves, and it
  wins any name collision against the tree below (the long-standing path).
- ``<workspace>/.claude/data/_direct/`` and ``<workspace>/.claude/data/
  <package_slug>/`` — reference files (symlink / hardlink / copy) the
  per-type stack sync writes, i.e. step 8 of ``agnes pull`` (called the
  ``v49 unified stack`` in older code comments). The reference FILENAME is
  the analyst-facing table name. `_rebuild_duckdb_views` also registers one
  view per name here (#1325, via `stack_reference_files` below), so a
  table reachable only from this tree is queryable too — `status` and the
  view rebuild share this module's model of the tree precisely so they
  cannot drift on what counts.
- ``<workspace>/.claude/data/_shared/`` — the canonical, content-addressed
  store the two reference trees above point INTO, keyed by
  ``table_registry.id`` rather than name. Never walked directly for view
  registration — it carries no analyst-facing name — so a `_shared` entry
  with no reference pointing at it (a broken sync, or one captured
  mid-write) is still not locally queryable. Pre-#1325, verified against a
  live workspace: 37 parquets in `_shared`, zero corresponding views, and
  `agnes query --local` on one of them failing with a DuckDB catalog error
  while the default server-side scope answered fine.

Keep the two counts distinct rather than summing them — a single number
either hides real bytes on disk or promises data `--local` cannot reach.
"""

from __future__ import annotations

import json
from pathlib import Path

_SHARED_DIRNAME = "_shared"
_SHARED_SUBPATH = (".claude", "data", _SHARED_DIRNAME)


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

    It is a heuristic and only a FALLBACK: Keboola auto-discovery builds the id
    from the *fully-qualified* source id (bucket included), so `id != slug(name)`
    there and this mapping cannot bridge it. `shared_id_to_name` reads the real
    relation from local state and is consulted first; this runs only when that
    state is missing or does not cover a stem.
    """
    return stem.strip().lower().replace(" ", "_")


def shared_id_to_name(workspace: Path) -> dict[str, str]:
    """``{table_id: table_name}`` from ``<workspace>/.claude/sync_state.json``.

    The authoritative relation, not a guess at it. The stack sync records one
    entry per synced table keyed by NAME, each carrying its registry
    ``table_id`` (`cli/lib/pull_sync.py`) — which is exactly the pair needed to
    match a `_shared/<id>.parquet` stem against a `server/parquet/<name>` entry.
    Necessary because slugifying the name does not reproduce the id for
    auto-discovered tables, where the id includes the source bucket.

    Missing, unreadable or malformed state yields an empty mapping, and callers
    fall back to `table_key`. That degrades to the old heuristic rather than
    failing, which matters because a workspace synced by an older CLI has no
    such file.
    """
    state_file = workspace / ".claude" / "sync_state.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}

    mapping: dict[str, str] = {}

    def _record(name: object, entry: object) -> None:
        if isinstance(entry, dict) and entry.get("table_id") and isinstance(name, str):
            mapping[str(entry["table_id"])] = name

    # Every level is shape-checked before being walked. The state file keys
    # these by name (`{name: entry}` / `{slug: {name: entry}}`), but the SERVER
    # manifest emits `direct_tables` / `data_packages` as ARRAYS under the same
    # names (`app/api/sync.py`), so a file carrying the manifest shape — or any
    # hand-edited or half-written one — would otherwise hit `.items()` on a list
    # and raise. `agnes status` does not guard this call, so that surfaced as
    # `AttributeError` with exit 1 and no output at all: strictly worse than a
    # wrong count, and the opposite of what this docstring promises.
    direct = state.get("direct_tables")
    if isinstance(direct, dict):
        for name, entry in direct.items():
            _record(name, entry)

    packages = state.get("data_packages")
    if isinstance(packages, dict):
        for package in packages.values():
            if isinstance(package, dict):
                for name, entry in package.items():
                    _record(name, entry)
    return mapping


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


def stack_reference_files(workspace: Path) -> dict[str, Path]:
    """``{table_name: reference_path}`` for every table reachable from the
    stack-sync tree: ``.claude/data/_direct/`` and every
    ``.claude/data/<package_slug>/`` (``_shared/`` itself excluded — it is
    the content-addressed store the references point INTO, and carries no
    analyst-facing name).

    The reference FILENAME is already the name: `cli/lib/pull_sync.py`
    writes `_direct/<name>.parquet` / `<package_slug>/<name>.parquet` keyed
    by the manifest's ``name`` field (through ``_safe_segment``), unlike
    `_shared/<table_id>.parquet`, which is keyed by the registry id — so the
    stem of a reference file needs no lookup (via `shared_id_to_name`) to
    resolve to the analyst-facing name; it already IS that name.

    Two packages carrying the same table produce two identically-named
    reference files — both pointing (by symlink, hardlink, or copy) at the
    same `_shared/<id>.parquet` — collapsed here to ONE entry per name so a
    caller registering one DuckDB view per key never double-registers
    (`cli/lib/pull.py::_register_stack_views`, #1325). Deterministic on a
    same-name collision (sorted directory walk, first entry wins) — safe
    because a same-name collision is, by construction, the same underlying
    table referenced twice.

    Missing/unreadable trees degrade to an empty mapping rather than raise —
    most workspaces predate the stack sync entirely, or haven't subscribed
    to anything yet.
    """
    refs: dict[str, Path] = {}
    data_dir = workspace / ".claude" / "data"
    if not data_dir.is_dir():
        return refs
    try:
        subdirs = list(data_dir.iterdir())
    except OSError:
        return refs
    for sub in sorted(subdirs, key=lambda p: p.name):
        if sub.name == _SHARED_DIRNAME or not sub.is_dir():
            continue
        try:
            entries = list(sub.glob("*.parquet"))
        except OSError:
            continue
        for entry in sorted(entries, key=lambda p: p.name):
            if not entry.is_file():
                continue
            refs.setdefault(entry.stem, entry)
    return refs


def count_local_tables(workspace: Path) -> tuple[int, int]:
    """``(queryable, downloaded_without_view)`` for this workspace.

    ``queryable`` counts tables the DuckDB view rebuild exposes: every name
    under ``server/parquet/`` PLUS every `stack_reference_files` name not
    already covered by one of those — mirroring `_rebuild_duckdb_views`'s
    own precedence (#1325), where `server/parquet/` (the long-standing path)
    wins a same-table collision and is therefore never double-counted here
    either. ``downloaded_without_view`` counts tables sitting in the
    stack-sync store (`_shared/`) with no counterpart in the queryable set —
    real bytes on disk that `--local` still cannot reach (e.g. a `_shared`
    parquet with no `_direct`/package reference pointing at it).
    """
    queryable = local_table_names(workspace / "server" / "parquet")
    queryable_keys = {table_key(n) for n in queryable}

    stack_names = stack_reference_files(workspace)
    extra_stack_names = {n for n in stack_names if table_key(n) not in queryable_keys}
    queryable_keys |= {table_key(n) for n in extra_stack_names}

    id_to_name = shared_id_to_name(workspace)

    unregistered = set()
    for stem in shared_store_stems(workspace):
        # Prefer the recorded id→name relation; `table_key` is the fallback for
        # workspaces whose state predates it or does not list this table.
        name = id_to_name.get(stem)
        if table_key(name if name is not None else stem) not in queryable_keys:
            unregistered.add(stem)
    return len(queryable) + len(extra_stack_names), len(unregistered)
