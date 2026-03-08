"""
Sync settings service for the webapp.

Reads/writes shared JSON files in /data/notifications/ to manage
user sync settings (which datasets to sync).
"""

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

NOTIFICATIONS_DIR = "/data/notifications"
SYNC_SETTINGS_FILE = os.path.join(NOTIFICATIONS_DIR, "sync_settings.json")

def _load_dataset_config():
    """Load dataset configuration from instance config."""
    try:
        from config.loader import load_instance_config, get_instance_value
        config = load_instance_config()
        datasets = get_instance_value(config, "datasets", default={})
        if datasets:
            defaults = {k: False for k in datasets}
            return defaults, datasets
    except Exception:
        pass
    # Fallback: empty (no optional datasets)
    return {}, {}


DEFAULT_SETTINGS, DATASET_INFO = _load_dataset_config()


def _read_json(path: str) -> dict:
    """Read a JSON file, return empty dict if not found or invalid."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(path: str, data: dict) -> None:
    """Write JSON data to file atomically."""
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o660)  # group-readable for data-ops
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


def get_sync_settings(username: str) -> dict[str, Any]:
    """Get sync settings for a user.

    Returns dict with:
    - datasets: {name: enabled} for each dataset
    - metadata: {name: {label, description, size_hint, requires}}
    """
    all_settings = _read_json(SYNC_SETTINGS_FILE)
    user_settings = all_settings.get(username, {})

    # Merge with defaults
    datasets = dict(DEFAULT_SETTINGS)
    datasets.update(user_settings.get("datasets", {}))

    return {
        "datasets": datasets,
        "metadata": DATASET_INFO,
        "updated_at": user_settings.get("updated_at"),
    }


def update_sync_settings(username: str, settings: dict) -> tuple[bool, str]:
    """Update sync settings for a user.

    Args:
        username: The username to update settings for
        settings: Dict with dataset names as keys and bool enabled as values

    Returns:
        (success, message) tuple
    """
    # Validate settings
    for key, value in settings.items():
        if key not in DEFAULT_SETTINGS:
            return False, f"Unknown dataset: {key}"
        if not isinstance(value, bool):
            return False, f"Invalid value for {key}: must be boolean"

    # Read current settings and merge (so partial updates don't reset other datasets)
    all_settings = _read_json(SYNC_SETTINGS_FILE)
    existing = all_settings.get(username, {}).get("datasets", dict(DEFAULT_SETTINGS))
    existing.update(settings)

    # Validate dependencies on merged state
    if existing.get("jira_attachments") and not existing.get("jira"):
        return False, "Jira attachments require Jira to be enabled"

    # Update user's settings
    all_settings[username] = {
        "datasets": existing,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Write back
    _write_json(SYNC_SETTINGS_FILE, all_settings)

    # Regenerate user's config file
    success = _regenerate_user_config(username, existing)
    if not success:
        logger.warning(f"Failed to regenerate config for {username}")
        # Don't fail - settings are saved, just config generation failed

    logger.info(f"Updated sync settings for '{username}': {existing}")
    return True, "Settings saved. Changes take effect on next sync."


def _regenerate_user_config(username: str, settings: dict) -> bool:
    """Regenerate ~/.sync_settings.yaml for a user on the server.

    Returns True on success, False on failure.
    """
    # Generate YAML content
    yaml_content = generate_user_config_yaml(settings)

    # Write to user's home directory on server
    user_config_path = f"/home/{username}/.sync_settings.yaml"

    try:
        # Use sudo to write as the target user
        # This requires webapp user to have sudoers entry for this specific operation
        # IMPORTANT: Must use /tmp/ explicitly - sudoers rule only allows /tmp/*.yaml
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, dir="/tmp") as f:
            f.write(yaml_content)
            tmp_path = f.name

        # Copy to user's home with correct ownership
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/install", "-o", username, "-g", username, "-m", "644", tmp_path, user_config_path],
            capture_output=True,
            text=True,
            timeout=10,
        )

        os.unlink(tmp_path)

        if result.returncode != 0:
            logger.error(f"Failed to install config for {username}: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout installing config for {username}")
        return False
    except Exception as e:
        logger.error(f"Error installing config for {username}: {e}")
        return False


def generate_user_config_yaml(settings: dict) -> str:
    """Generate YAML content for sync config.

    Args:
        settings: Dict with dataset names and enabled status

    Returns:
        YAML string content
    """
    lines = [
        "# Data Analyst - Sync Configuration",
        "# Managed by web portal - changes here may be overwritten",
        "",
        "datasets:",
    ]

    for dataset, enabled in sorted(settings.items()):
        value = "true" if enabled else "false"
        lines.append(f"  {dataset}: {value}")

    lines.append("")
    return "\n".join(lines)
