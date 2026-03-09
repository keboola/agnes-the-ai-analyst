"""
Advisory file locking for Jira read-modify-write operations.

Two lock granularities:
- issue_json_lock: per-issue lock for JSON read-modify-write (webhook/SLA poll)
- parquet_month_lock: per-month lock for Parquet read-modify-write (transform)

Lock nesting order (always outer → inner to prevent deadlocks):
    issue_json_lock(issue_key)           ← outer (webhook/SLA poll)
      └── parquet_month_lock(month_key)  ← inner (transform)

Uses fcntl.flock() for POSIX advisory locking (works across processes).

Usage:
    from connectors.jira.file_lock import issue_json_lock, parquet_month_lock

    with issue_json_lock(issues_dir, "SUPPORT-1234"):
        # read JSON, modify, write
        ...
        with parquet_month_lock(output_dir, "2025-01"):
            # read Parquet, upsert, write
            ...
"""

import fcntl
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)


@contextmanager
def issue_json_lock(
    issues_dir: Path, issue_key: str
) -> Generator[None, None, None]:
    """
    Acquire an advisory file lock for a specific Jira issue.

    Lock files are stored in {issues_dir}/.locks/{issue_key}.lock.
    The lock is exclusive (LOCK_EX) and blocking - it will wait until
    the lock is available.

    Args:
        issues_dir: Directory containing issue JSON files (e.g., /data/.../issues)
        issue_key: Jira issue key (e.g., "SUPPORT-1234")

    Yields:
        None - the lock is held for the duration of the with block
    """
    locks_dir = issues_dir / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)

    lock_path = locks_dir / f"{issue_key}.lock"

    fd = open(lock_path, "w")
    try:
        logger.debug(f"Acquiring lock for {issue_key}")
        fcntl.flock(fd, fcntl.LOCK_EX)
        logger.debug(f"Lock acquired for {issue_key}")
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
        logger.debug(f"Lock released for {issue_key}")


@contextmanager
def parquet_month_lock(
    output_dir: Path, month_key: str
) -> Generator[None, None, None]:
    """
    Acquire an advisory file lock for a monthly Parquet partition.

    Serializes all read-modify-write operations on the same month's Parquet
    files across all 6 tables. Different months are not blocked.

    Lock files are stored in {output_dir}/.locks/parquet-{month_key}.lock.
    The lock is exclusive (LOCK_EX) and blocking.

    Args:
        output_dir: Parquet output directory (e.g., /data/src_data/parquet/jira)
        month_key: Month partition key (e.g., "2025-01")

    Yields:
        None - the lock is held for the duration of the with block
    """
    locks_dir = output_dir / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)

    lock_path = locks_dir / f"parquet-{month_key}.lock"

    fd = open(lock_path, "w")
    try:
        logger.debug(f"Acquiring parquet lock for {month_key}")
        fcntl.flock(fd, fcntl.LOCK_EX)
        logger.debug(f"Parquet lock acquired for {month_key}")
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
        logger.debug(f"Parquet lock released for {month_key}")
