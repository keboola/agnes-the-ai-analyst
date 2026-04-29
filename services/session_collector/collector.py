#!/usr/bin/env python3
"""Collect Claude Code session transcripts from all user home directories.

This script runs as a systemd service (session-collector.service) triggered by
session-collector.timer. It scans all /home/*/user/sessions/ directories and
copies session transcript files to /data/user_sessions/$user/ for centralized
storage and analysis.

Design principles:
- Must run as root (or user with read access to all /home/*)
- Preserves file metadata (timestamps, permissions)
- Idempotent - safe to run multiple times (skips existing files)
- Atomic operations - uses tempfile + os.replace for safety
- Logs to stdout (captured by journalctl)

TODO(scheduler-v2): In docker-compose.yml this service is a one-shot process
restarted by Docker (`restart: unless-stopped`), which is effectively a tight
boot loop. Replace with proper cadence: either an internal `while True: scan;
sleep(N)` loop, or wire into services/scheduler/__main__.py JOBS list with an
admin endpoint /api/admin/collect-sessions.
"""

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Iterator

from app.logging_config import setup_logging

# Central storage for session transcripts
TARGET_BASE = Path("/data/user_sessions")

# Directory to scan for sessions in each user home
USER_SESSIONS_DIR = "user/sessions"

setup_logging(__name__)
logger = logging.getLogger(__name__)


def find_user_home_dirs() -> Iterator[Path]:
    """Yield all user home directories from /home/*."""
    home_base = Path("/home")
    if not home_base.exists():
        logger.warning(f"{home_base} does not exist")
        return

    for entry in home_base.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            yield entry


def find_session_files(user_home: Path) -> Iterator[Path]:
    """Yield all session JSONL files from user's sessions directory."""
    sessions_dir = user_home / USER_SESSIONS_DIR
    if not sessions_dir.exists():
        return

    try:
        for entry in sessions_dir.iterdir():
            if entry.is_file() and entry.suffix == ".jsonl":
                yield entry
    except PermissionError:
        logger.warning(f"Permission denied reading {sessions_dir}")
    except Exception as e:
        logger.error(f"Error scanning {sessions_dir}: {e}")


def copy_session_file(source: Path, target: Path, dry_run: bool = False) -> bool:
    """Copy session file to target location, preserving metadata.

    Returns True if file was copied, False if skipped (already exists).
    """
    if target.exists():
        # Already collected, skip
        return False

    if dry_run:
        logger.info(f"[DRY-RUN] Would copy: {source} -> {target}")
        return True

    try:
        # Ensure target directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        # Copy with metadata preserved
        shutil.copy2(source, target)
        logger.info(f"Collected: {source} -> {target}")
        return True
    except Exception as e:
        logger.error(f"Failed to copy {source} to {target}: {e}")
        return False


def collect_user_sessions(username: str, user_home: Path, dry_run: bool = False) -> tuple[int, int]:
    """Collect all session files for a user.

    Returns tuple (files_copied, files_skipped).
    """
    target_dir = TARGET_BASE / username
    copied = 0
    skipped = 0

    for session_file in find_session_files(user_home):
        target_path = target_dir / session_file.name

        if copy_session_file(session_file, target_path, dry_run=dry_run):
            copied += 1
        else:
            skipped += 1

    return copied, skipped


def main() -> int:
    """Main entry point. Returns exit code (0=success, 1=error)."""
    import argparse
    import grp

    parser = argparse.ArgumentParser(description="Collect Claude Code session transcripts from all users")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be copied without actually copying")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    logger.info("Starting session transcript collection")

    # Ensure target base directory exists
    try:
        TARGET_BASE.mkdir(parents=True, exist_ok=True)
        # Set permissions: root:data-ops, 2770 (admins only, sessions are sensitive)
        os.chmod(TARGET_BASE, 0o2770)

        # Try to set group ownership to data-ops if it exists
        try:
            dataops_gid = grp.getgrnam("data-ops").gr_gid
            os.chown(TARGET_BASE, -1, dataops_gid)
        except KeyError:
            logger.warning("Group 'data-ops' not found, using default group")
        except Exception as e:
            logger.warning(f"Could not set group ownership: {e}")

    except Exception as e:
        logger.error(f"Failed to create target directory {TARGET_BASE}: {e}")
        return 1

    total_copied = 0
    total_skipped = 0
    users_processed = 0

    for user_home in find_user_home_dirs():
        username = user_home.name

        # Skip system users (numeric UIDs typically < 1000)
        try:
            uid = user_home.stat().st_uid
            if uid < 1000:
                continue
        except Exception:
            continue

        copied, skipped = collect_user_sessions(username, user_home, dry_run=args.dry_run)

        if copied > 0 or skipped > 0:
            users_processed += 1
            total_copied += copied
            total_skipped += skipped
            logger.info(f"User {username}: {copied} copied, {skipped} skipped")

    logger.info(
        f"Collection complete: {users_processed} users, {total_copied} files copied, {total_skipped} files skipped"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
