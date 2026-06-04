"""Shared utilities for the FastAPI application."""
import os
from pathlib import Path


def get_data_dir() -> Path:
    """Return the configured data directory path."""
    return Path(os.environ.get("DATA_DIR", "./data"))


def resolve_local_parquet(table_id: str, source_type: str | None = None) -> Path | None:
    """Resolve the on-disk parquet for a local/materialized table.

    The v2 extract.duckdb contract lays parquets out at
    ``${DATA_DIR}/extracts/<source>/data/<table_id>.parquet`` where ``<source>``
    is the extract DIRECTORY NAME the orchestrator scanned — which is NOT
    necessarily equal to the registry ``source_type``. Built-in connectors
    happen to use a directory named after their source_type
    (``keboola``/``bigquery``), but a generic extract.duckdb may live under any
    directory name: e.g. the bundled ``demo`` extract registers its tables with
    ``source_type='local'`` while its parquets live under ``extracts/demo/``.
    Keying the path off ``source_type`` therefore looked up
    ``extracts/local/data/<id>.parquet`` (nonexistent) and crashed ``read_parquet``.

    Resolve by searching for ``data/<table_id>.parquet`` anywhere under the
    extracts tree — the same source-name-agnostic lookup ``app/api/catalog.py``
    and ``app/api/data.py`` already use. ``source_type``, when supplied, is
    tried first as a fast path (preserves the historical layout/behavior for
    built-in connectors and disambiguates the rare case of the same table_id
    appearing under two sources). Returns ``None`` when no parquet exists.
    """
    extracts = get_data_dir() / "extracts"
    if not extracts.exists():
        return None
    if source_type:
        direct = extracts / source_type / "data" / f"{table_id}.parquet"
        if direct.exists():
            return direct
    matches = list(extracts.rglob(f"data/{table_id}.parquet"))
    return matches[0] if matches else None


def get_marketplaces_dir() -> Path:
    """Path where marketplace git repos are cloned by the nightly sync."""
    return get_data_dir() / "marketplaces"


def get_marketplace_cache_dir() -> Path:
    """Root for the curated-marketplace external-asset mirror.

    Each registered marketplace gets a sub-directory keyed by slug holding a
    ``manifest.json`` and one file per mirrored URL. Lives outside the cloned
    git working tree so its contents don't interfere with ``git status`` /
    ``git fetch --depth 1 ; git reset --hard`` semantics. Cleaned up
    alongside the working tree on marketplace unregister
    (``src.marketplace.delete_marketplace_dir``).
    """
    return get_data_dir() / "marketplace-cache"


def get_initial_workspace_dir() -> Path:
    """Path where the admin-configured Initial Workspace Template is cloned.

    Singleton (one per instance) — admin registers the repo via
    /admin/server-config → "Initial Workspace Template" section. Used by
    ``src.initial_workspace`` to clone/fetch and to serve via
    ``/api/initial-workspace.zip``. Layout:

        ${DATA_DIR}/initial-workspace/      ← git working copy
            .git/                            ← present on disk, excluded from zip
            CLAUDE.md, .claude/, ...         ← analyst workspace content
    """
    return get_data_dir() / "initial-workspace"


def get_store_dir() -> Path:
    """Root for community-uploaded Store entities.

    Layout:
        ${DATA_DIR}/store/<entity_id>/plugin/   ← canonical Claude Code plugin tree
        ${DATA_DIR}/store/<entity_id>/assets/   ← photo + docs
    """
    return get_data_dir() / "store"
