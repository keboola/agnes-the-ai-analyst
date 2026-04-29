"""Shared utilities for the FastAPI application."""
import os
from pathlib import Path


def get_data_dir() -> Path:
    """Return the configured data directory path."""
    return Path(os.environ.get("DATA_DIR", "./data"))


def get_marketplaces_dir() -> Path:
    """Path where marketplace git repos are cloned by the nightly sync."""
    return get_data_dir() / "marketplaces"
