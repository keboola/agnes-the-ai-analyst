"""Instance configuration — loads instance.yaml and exposes to FastAPI."""

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_instance_config: Optional[dict] = None


def reset_cache() -> None:
    """Drop the in-process instance.yaml cache; the next ``load_instance_config``
    call re-reads from disk. Used by `/api/admin/server-config` after a save.
    Public alias so callers don't have to reach into the private global.

    Also clears ``connectors.bigquery.access.get_bq_access`` so the v2 endpoints
    pick up new BigQuery project IDs after an admin saves `instance.yaml` —
    without this, `get_bq_access`'s `@functools.cache` would freeze the projects
    at first call and require a container restart to pick up changes (Devin
    ANALYSIS_0004 on PR #138). Lazy-imported so this module stays usable in
    environments where the connectors package can't be imported (e.g. unit
    tests of instance_config in isolation)."""
    global _instance_config
    _instance_config = None
    try:
        from connectors.bigquery.access import get_bq_access
        get_bq_access.cache_clear()
    except Exception:
        # Connectors module not loaded yet, or BQ deps missing — both fine.
        pass


def _deep_merge(base: dict, patch: dict) -> dict:
    """Deep-merge `patch` into `base`, returning a new dict.

    Dict-into-dict recurses; everything else (scalars, lists, None) is
    replaced wholesale. Used so the writable overlay can hold only the
    sections an operator has touched, while everything else flows from
    the static file unchanged. Same semantics as the helper in
    `/api/admin/server-config`'s POST handler.
    """
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_instance_config() -> dict:
    """Load instance.yaml as a deep-merge of the static file and the
    writable overlay.

    Resolution:
    1. Static base: ``CONFIG_DIR/instance.yaml`` via ``config.loader``
       (the source of truth for sections the editor doesn't expose —
       ``datasets``, ``corporate_memory``, ``openmetadata``, etc.).
    2. Overlay patch: ``DATA_DIR/state/instance.yaml`` (written by
       ``/api/admin/configure`` and ``/api/admin/server-config``;
       contains only the sections those endpoints accept).
    3. Overlay wins per-leaf via deep-merge — operator edits persist,
       static-only sections still flow through.

    Pre-2026-04-28 this function returned the overlay verbatim when it
    existed and only fell back to static when it didn't. That was a
    silent footgun: the moment someone saved any section through the
    new editor (which writes a narrow overlay by design), every
    consumer of static-only sections (corporate memory page, dataset
    list, OpenMetadata client) saw empty defaults. See PR #107.
    """
    global _instance_config
    if _instance_config is not None:
        return _instance_config

    import yaml

    # Static base — strict validation lives in config.loader.
    base: dict = {}
    try:
        from config.loader import load_instance_config as _load
        base = _load() or {}
        logger.info("Loaded instance.yaml base from config/")
    except Exception as e:
        logger.warning(f"Could not load static instance.yaml: {e}")

    # Overlay patch from the writable volume. Best-effort — a corrupt
    # overlay shouldn't take the app offline (we'd rather serve stale/base
    # config than 500 every request), but log loudly with a traceback so
    # the corruption surfaces in the operator's logs immediately. The
    # write-side endpoints (POST /api/admin/server-config and /configure)
    # refuse to overwrite a corrupt overlay with HTTP 500, so an admin
    # noticing the saves break is the second line of defence.
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    overlay_path = data_dir / "state" / "instance.yaml"
    if overlay_path.exists():
        try:
            overlay = yaml.safe_load(overlay_path.read_text()) or {}
            base = _deep_merge(base, overlay)
            logger.info("Merged overlay from %s", overlay_path)
        except Exception:
            logger.exception(
                "instance.yaml overlay at %s is corrupt — falling back to "
                "static base config; saves through the editor will refuse "
                "until the file is repaired", overlay_path,
            )

    _instance_config = base
    return _instance_config


def get_value(*keys, default=None) -> Any:
    """Get nested value from instance config."""
    config = load_instance_config()
    current = config
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
        if current is None:
            return default
    return current


def get_data_source_type() -> str:
    return os.environ.get("DATA_SOURCE", get_value("data_source", "type", default="local"))


def get_instance_name() -> str:
    return get_value("instance", "name", default="AI Data Analyst")


def get_instance_subtitle() -> str:
    return get_value("instance", "subtitle", default="")


def get_sync_interval() -> str:
    """Human-readable refresh cadence shown in the analyst welcome prompt."""
    return get_value("instance", "sync_interval", default="1 hour")


def get_allowed_domains() -> list:
    domain = get_value("auth", "allowed_domain", default="")
    if domain:
        return [d.strip() for d in domain.split(",") if d.strip()]
    return []


def get_datasets() -> dict:
    return get_value("datasets", default={})


def get_theme() -> dict:
    return get_value("theme", default={})


def get_auth_config() -> dict:
    return get_value("auth", default={})


def get_corporate_memory_config() -> dict:
    return get_value("corporate_memory", default={})
