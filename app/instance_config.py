"""Instance configuration — loads instance.yaml and exposes to FastAPI."""

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_instance_config: Optional[dict] = None


def load_instance_config() -> dict:
    """Load instance.yaml — checks API-generated config first, then static config.

    Search order:
    1. DATA_DIR/state/instance.yaml (written by /api/admin/configure, writable)
    2. CONFIG_DIR/instance.yaml (static, read-only in Docker)
    3. Empty dict with defaults (if neither exists)
    """
    global _instance_config
    if _instance_config is not None:
        return _instance_config

    # First, try API-generated config in writable data volume
    import yaml
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    api_config_path = data_dir / "state" / "instance.yaml"
    if api_config_path.exists():
        try:
            _instance_config = yaml.safe_load(api_config_path.read_text()) or {}
            logger.info("Loaded instance.yaml from %s", api_config_path)
            return _instance_config
        except Exception as e:
            logger.warning(f"Could not load API-generated instance.yaml: {e}")

    # Fall back to static config (may have strict validation)
    try:
        from config.loader import load_instance_config as _load
        _instance_config = _load()
        logger.info("Loaded instance.yaml from config/")
    except Exception as e:
        logger.warning(f"Could not load instance.yaml: {e}. Using defaults.")
        _instance_config = {}

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
