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

    # Validate dependencies on merged state (from instance config)
    for key, info in DATASET_INFO.items():
        requires = info.get("requires") if isinstance(info, dict) else None
        if requires and existing.get(key) and not existing.get(requires):
            return False, f"{key} requires {requires} to be enabled"

    # Preserve existing table subscription settings
    existing_user = all_settings.get(username, {})
    table_mode = existing_user.get("table_mode", "all")
    table_settings = existing_user.get("tables", {})

    # Update user's settings
    all_settings[username] = {
        "datasets": existing,
        "table_mode": table_mode,
        "tables": table_settings,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Write back
    _write_json(SYNC_SETTINGS_FILE, all_settings)

    # Regenerate user's config file (with table settings)
    success = _regenerate_user_config(username, existing, table_mode, table_settings)
    if not success:
        logger.warning(f"Failed to regenerate config for {username}")
        # Don't fail - settings are saved, just config generation failed

    logger.info(f"Updated sync settings for '{username}': {existing}")
    return True, "Settings saved. Changes take effect on next sync."


def _regenerate_user_config(username: str, settings: dict, table_mode: str = "all", table_settings: dict | None = None) -> bool:
    """Regenerate ~/.sync_settings.yaml and ~/.sync_rsync_filter for a user on the server.

    Returns True on success, False on failure.
    """
    # Generate YAML content
    yaml_content = generate_user_config_yaml(settings, table_mode, table_settings)

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

        # Generate and write rsync filter file
        filter_ok = _write_rsync_filter(username, settings, table_mode, table_settings or {})
        if not filter_ok:
            logger.warning(f"Failed to write rsync filter for {username}")
            # Don't fail overall - YAML config was written successfully

        return True

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout installing config for {username}")
        return False
    except Exception as e:
        logger.error(f"Error installing config for {username}: {e}")
        return False


def _write_rsync_filter(username: str, dataset_settings: dict, table_mode: str, table_settings: dict) -> bool:
    """Write ~/.sync_rsync_filter for a user on the server.

    Returns True on success, False on failure.
    """
    # Load folder_mapping from table registry (or instance config as fallback)
    folder_mapping = {}
    try:
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        conn = get_system_db()
        repo = TableRegistryRepository(conn)
        tables = repo.list_all()
        folder_mapping = {t["bucket"]: t["folder"] for t in tables if t.get("bucket") and t.get("folder")}
    except Exception:
        try:
            from config.loader import load_instance_config, get_instance_value
            config = load_instance_config()
            folder_mapping = get_instance_value(config, "folder_mapping", default={})
        except Exception:
            pass

    # Generate filter content
    filter_content = generate_rsync_filter(dataset_settings, table_mode, table_settings, folder_mapping)

    user_filter_path = f"/home/{username}/.sync_rsync_filter"

    try:
        # Write filter to temp file, then install to user's home
        # IMPORTANT: Must use /tmp/ explicitly - sudoers rule restrictions
        with tempfile.NamedTemporaryFile(mode="w", suffix=".filter", delete=False, dir="/tmp") as f:
            f.write(filter_content)
            tmp_path = f.name

        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/install", "-o", username, "-g", username, "-m", "644", tmp_path, user_filter_path],
            capture_output=True,
            text=True,
            timeout=10,
        )

        os.unlink(tmp_path)

        if result.returncode != 0:
            logger.error(f"Failed to install rsync filter for {username}: {result.stderr}")
            return False

        return True

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout installing rsync filter for {username}")
        return False
    except Exception as e:
        logger.error(f"Error installing rsync filter for {username}: {e}")
        return False


def get_table_subscriptions(username: str) -> dict:
    """Get per-table subscription settings for a user.

    Returns:
        {"table_mode": "all"|"explicit", "tables": {"name": bool, ...}}
    """
    all_settings = _read_json(SYNC_SETTINGS_FILE)
    user_settings = all_settings.get(username, {})

    return {
        "table_mode": user_settings.get("table_mode", "all"),
        "tables": user_settings.get("tables", {}),
    }


def update_table_subscriptions(username: str, table_mode: str, table_settings: dict) -> tuple[bool, str]:
    """Update per-table subscriptions for a user.

    Args:
        username: The username
        table_mode: "all" or "explicit"
        table_settings: Dict with table names as keys and bool as values

    Returns:
        (success, message)
    """
    # Validate table_mode
    if table_mode not in ("all", "explicit"):
        return False, f"Invalid table_mode: {table_mode}. Must be 'all' or 'explicit'"

    # Validate table_settings values
    for key, value in table_settings.items():
        if not isinstance(value, bool):
            return False, f"Invalid value for table '{key}': must be boolean"

    # Read current settings and update
    all_settings = _read_json(SYNC_SETTINGS_FILE)
    if username not in all_settings:
        all_settings[username] = {}

    all_settings[username]["table_mode"] = table_mode
    all_settings[username]["tables"] = table_settings
    all_settings[username]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Write back
    _write_json(SYNC_SETTINGS_FILE, all_settings)

    # Regenerate user's config file (with dataset + table settings)
    dataset_settings = all_settings[username].get("datasets", dict(DEFAULT_SETTINGS))
    success = _regenerate_user_config(username, dataset_settings, table_mode, table_settings)
    if not success:
        logger.warning(f"Failed to regenerate config for {username}")

    logger.info(f"Updated table subscriptions for '{username}': mode={table_mode}, tables={table_settings}")
    return True, "Table subscriptions saved. Changes take effect on next sync."


def generate_rsync_filter(dataset_settings: dict, table_mode: str, table_settings: dict, folder_mapping: dict) -> str:
    """Generate rsync filter file content for per-table sync.

    Args:
        dataset_settings: {"jira": True, ...}
        table_mode: "all" or "explicit"
        table_settings: {"company": True, "events": False, ...}
        folder_mapping: {"in.c-crm": "crm", ...} from registry/config

    Returns:
        Rsync filter file content string.
    """
    if table_mode == "all":
        # No filtering needed - include everything
        lines = [
            "# AUTO-GENERATED rsync filter for per-table sync",
            "# table_mode: all",
            "",
            "# No filtering - all tables included",
        ]
        return "\n".join(lines) + "\n"

    lines = [
        "# AUTO-GENERATED rsync filter for per-table sync",
        "# table_mode: explicit",
        "",
    ]

    # Build reverse mapping: table_name -> folder
    # We need to know which folder each table lives in
    # folder_mapping is bucket_id -> folder_name
    # We'll collect all unique folders
    folders_used = set(folder_mapping.values()) if folder_mapping else set()

    # Subscribed tables
    subscribed = {name for name, enabled in table_settings.items() if enabled}
    unsubscribed = {name for name, enabled in table_settings.items() if not enabled}

    if subscribed:
        lines.append("# Subscribed tables")
        for name in sorted(subscribed):
            # Include parquet file and partitioned directory
            lines.append(f"+ **/{name}.parquet")
            lines.append(f"+ **/{name}/***")
        lines.append("")

    if unsubscribed:
        lines.append("# Excluded tables")
        for name in sorted(unsubscribed):
            lines.append(f"- **/{name}.parquet")
            lines.append(f"- **/{name}/***")
        lines.append("")

    # Include folder structure but exclude unknown files
    lines.append("# Include folder structure")
    lines.append("+ */")
    lines.append("- *")
    lines.append("")

    return "\n".join(lines)


def generate_user_config_yaml(settings: dict, table_mode: str = "all", table_settings: dict | None = None) -> str:
    """Generate YAML content for sync config.

    Args:
        settings: Dict with dataset names and enabled status
        table_mode: "all" or "explicit" (default "all")
        table_settings: Dict with table names and subscription status (optional)

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

    # Per-table subscriptions
    lines.append(f"table_mode: {table_mode}")

    if table_settings:
        lines.append("tables:")
        for table_name, subscribed in sorted(table_settings.items()):
            value = "true" if subscribed else "false"
            lines.append(f"  {table_name}: {value}")
    else:
        lines.append("tables: {}")

    lines.append("")
    return "\n".join(lines)
