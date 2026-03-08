"""
Instance configuration loader.

Loads instance.yaml from CONFIG_DIR (env var) or ./config/ fallback.
Used by both webapp and src modules for instance-specific settings.
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "./config"))


def load_instance_config() -> dict[str, Any]:
    """Load instance configuration from instance.yaml.

    Search order:
    1. $CONFIG_DIR/instance.yaml
    2. ./config/instance.yaml

    Raises:
        FileNotFoundError: If instance.yaml not found.
        yaml.YAMLError: If YAML is invalid.
        ValueError: If config is empty or missing required fields.
    """
    path = CONFIG_DIR / "instance.yaml"
    if not path.exists():
        # Fallback to local config dir
        path = Path("./config/instance.yaml")

    if not path.exists():
        raise FileNotFoundError(
            "Instance configuration not found. "
            "Copy config/instance.yaml.example to config/instance.yaml "
            "and fill in your values."
        )

    with open(path) as f:
        config = yaml.safe_load(f)

    if not config:
        raise ValueError("instance.yaml is empty")

    _validate_config(config)
    logger.info("Instance config loaded from %s", path)
    return config


def _validate_config(config: dict) -> None:
    """Validate required configuration fields.

    Raises:
        ValueError: If required fields are missing or empty.
    """
    required_paths = [
        ("instance", "name"),
        ("auth", "allowed_domain"),
        ("server", "host"),
        ("server", "hostname"),
    ]

    missing = []
    for keys in required_paths:
        value = config
        path_str = ".".join(keys)
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                missing.append(path_str)
                break
            value = value[key]
        else:
            if not value:
                missing.append(path_str)

    if missing:
        raise ValueError(
            f"Missing required instance config fields: {', '.join(missing)}. "
            f"Check config/instance.yaml"
        )


def get_instance_value(config: dict, *keys: str, default: Any = None) -> Any:
    """Get a nested value from instance config.

    Args:
        config: Instance config dict.
        *keys: Path of keys (e.g., "instance", "name").
        default: Default value if path not found.

    Returns:
        Config value or default.
    """
    value = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value if value is not None else default
