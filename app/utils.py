"""Shared utilities for the FastAPI application."""
import os
from pathlib import Path


def get_data_dir() -> Path:
    """Return the configured data directory path."""
    return Path(os.environ.get("DATA_DIR", "./data"))


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
