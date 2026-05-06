"""Shared utilities for the FastAPI application."""
import os
from pathlib import Path


def get_data_dir() -> Path:
    """Return the configured data directory path."""
    return Path(os.environ.get("DATA_DIR", "./data"))


def get_marketplaces_dir() -> Path:
    """Path where marketplace git repos are cloned by the nightly sync."""
    return get_data_dir() / "marketplaces"


def get_store_dir() -> Path:
    """Root for community-uploaded Store entities.

    Layout:
        ${DATA_DIR}/store/<entity_id>/plugin/   ← canonical Claude Code plugin tree
        ${DATA_DIR}/store/<entity_id>/assets/   ← photo + docs
    """
    return get_data_dir() / "store"
