#!/usr/bin/env python3
"""
Generate per-user sync config files from central sync_settings.json.

This script reads /data/notifications/sync_settings.json and writes
~/.sync_settings.yaml for each user with their configured settings.

Usage:
    python3 generate_user_sync_configs.py [--dry-run]

Run this script:
- After manual changes to sync_settings.json
- As a cron job to ensure configs stay in sync
- The webapp calls this automatically after settings changes
"""

import argparse
import json
import os
import pwd
import sys
from pathlib import Path

SYNC_SETTINGS_FILE = Path("/data/notifications/sync_settings.json")
DEFAULT_SETTINGS = {
    "jira": False,
    "jira_attachments": False,
}


def read_settings() -> dict:
    """Read sync settings from JSON file."""
    if not SYNC_SETTINGS_FILE.exists():
        return {}
    try:
        with open(SYNC_SETTINGS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error reading {SYNC_SETTINGS_FILE}: {e}", file=sys.stderr)
        return {}


def generate_yaml(settings: dict) -> str:
    """Generate YAML content for a user's sync config."""
    lines = [
        "# AI Data Analyst - Data Sync Configuration",
        "# Managed by web portal - changes here may be overwritten",
        "#",
        "# To manage settings, visit the web portal (Data Settings page)",
        "",
        "datasets:",
    ]

    for dataset, enabled in sorted(settings.items()):
        value = "true" if enabled else "false"
        lines.append(f"  {dataset}: {value}")

    lines.append("")
    return "\n".join(lines)


def get_user_home(username: str) -> Path | None:
    """Get home directory for a user."""
    try:
        return Path(pwd.getpwnam(username).pw_dir)
    except KeyError:
        return None


def write_user_config(username: str, yaml_content: str, dry_run: bool = False) -> bool:
    """Write config file to user's home directory.

    Returns True on success, False on failure.
    """
    home = get_user_home(username)
    if not home:
        print(f"  [SKIP] User {username} not found on system")
        return False

    config_path = home / ".sync_settings.yaml"

    if dry_run:
        print(f"  [DRY-RUN] Would write {config_path}")
        return True

    try:
        # Write to temp file first
        import tempfile

        fd, tmp_path = tempfile.mkstemp(suffix=".yaml", dir=str(home.parent))
        try:
            with os.fdopen(fd, "w") as f:
                f.write(yaml_content)

            # Get user's uid/gid
            user_info = pwd.getpwnam(username)

            # Set ownership
            os.chown(tmp_path, user_info.pw_uid, user_info.pw_gid)
            os.chmod(tmp_path, 0o644)

            # Atomic move
            os.replace(tmp_path, config_path)

            print(f"  [OK] {config_path}")
            return True

        except Exception as e:
            os.unlink(tmp_path)
            raise e

    except PermissionError:
        print(f"  [ERROR] Permission denied for {username}")
        return False
    except Exception as e:
        print(f"  [ERROR] {username}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Generate per-user sync config files from central settings."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without making changes"
    )
    parser.add_argument("--user", help="Generate config for specific user only")
    args = parser.parse_args()

    all_settings = read_settings()

    if not all_settings:
        print("No sync settings found or file is empty.")
        return 0

    if args.user:
        users = {args.user: all_settings.get(args.user, {})}
    else:
        users = all_settings

    print(f"Generating sync configs for {len(users)} user(s)...")
    success = 0
    failed = 0

    for username, user_data in users.items():
        # Merge with defaults
        settings = dict(DEFAULT_SETTINGS)
        settings.update(user_data.get("datasets", {}))

        yaml_content = generate_yaml(settings)

        if write_user_config(username, yaml_content, args.dry_run):
            success += 1
        else:
            failed += 1

    print(f"\nDone: {success} succeeded, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
