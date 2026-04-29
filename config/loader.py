"""
Instance configuration loader.

Loads instance.yaml from CONFIG_DIR (env var) or ./config/ fallback.
Used by both webapp and src modules for instance-specific settings.

Supports ${ENV_VAR} syntax in YAML values for secret interpolation.
Actual secret values are stored in .env (gitignored), while the YAML
structure stays in instance.yaml.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "./config"))

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")

SUPPORTED_CONFIG_VERSIONS = {1}


def _resolve_env_refs(value: Any, _path: str = "") -> Any:
    """Resolve ${ENV_VAR} references in config values.

    Walks the config tree recursively. String values containing ${VAR}
    are replaced with the corresponding environment variable value.
    Logs a warning for unset variables so misconfiguration is visible.
    Non-string values pass through unchanged.
    """
    if isinstance(value, str):
        missing_vars: list[str] = []

        def replacer(match: re.Match) -> str:
            env_key = match.group(1)
            env_val = os.environ.get(env_key)
            if env_val is None:
                missing_vars.append(env_key)
                return ""
            return env_val

        resolved = _ENV_PATTERN.sub(replacer, value)
        for var in missing_vars:
            logger.warning(
                "Environment variable %s not set (referenced in config %s)",
                var,
                _path or "value",
            )
        return resolved
    if isinstance(value, dict):
        return {
            k: _resolve_env_refs(v, _path=f"{_path}.{k}" if _path else k)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_env_refs(item, _path=f"{_path}[{i}]")
            for i, item in enumerate(value)
        ]
    return value


def _validate_config_version(config: dict) -> None:
    """Validate config_version field in the loaded config.

    Reads config_version from the config dict. If missing, logs a warning
    and defaults to 0. If the version is not in SUPPORTED_CONFIG_VERSIONS,
    raises an error with a clear message.

    Raises:
        ValueError: If config_version is not supported.
    """
    version = config.get("config_version")
    if version is None:
        logger.warning(
            "config_version not set in instance.yaml; defaulting to 0. "
            "Add config_version: 1 to your config for forward compatibility."
        )
        version = 0
    if version not in SUPPORTED_CONFIG_VERSIONS:
        raise ValueError(
            f"Unsupported config_version: {version}. "
            f"Supported versions: {sorted(SUPPORTED_CONFIG_VERSIONS)}. "
            f"Update your instance.yaml config_version field."
        )


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

    config = _resolve_env_refs(config)
    _validate_config_version(config)
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

    # Secret fields that must resolve to non-empty values (from .env)
    required_secrets = [
        ("auth", "webapp_secret_key"),
    ]

    missing = []
    for keys in required_paths + required_secrets:
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
            f"Check config/instance.yaml and .env"
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
