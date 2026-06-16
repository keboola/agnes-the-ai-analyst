"""
Incremental Jira transform - update single issue in Parquet files.

Called by webhook handler after issue JSON and attachments are saved.
Updates only the affected monthly Parquet file for efficient rsync.
"""

import json
import logging
import os
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

# Import transform functions from batch transform
from .file_lock import parquet_month_lock
from .validation import is_valid_issue_key, safe_join_under
from .transform import (
    ATTACHMENTS_SCHEMA,
    CHANGELOG_SCHEMA,
    COMMENTS_SCHEMA,
    HIVE_PARTITION_PREFIX,
    ISSUES_SCHEMA,
    ISSUELINKS_SCHEMA,
    PARQUET_WRITE_OPTIONS,
    REMOTE_LINKS_SCHEMA,
    apply_schema,
    get_month_key,
    transform_attachments,
    transform_changelog,
    transform_comments,
    transform_issue,
    transform_issuelinks,
    transform_remote_links,
)

logger = logging.getLogger(__name__)

# Default paths (can be overridden via environment)
DEFAULT_RAW_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "extracts" / "jira" / "raw"
DEFAULT_OUTPUT_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "extracts" / "jira" / "data"


def upsert_dataframe(
    existing_df: pd.DataFrame | None,
    new_records: list[dict],
    key_column: str,
    issue_key: str,
) -> pd.DataFrame:
    """
    Upsert new records into existing DataFrame.

    - Removes all rows matching issue_key
    - Adds new records

    Args:
        existing_df: Existing DataFrame (or None if new file)
        new_records: List of new records to add
        key_column: Column used for matching (e.g., 'issue_key')
        issue_key: Issue key to remove/replace

    Returns:
        Updated DataFrame
    """
    new_df = pd.DataFrame(new_records) if new_records else pd.DataFrame()

    if existing_df is None or existing_df.empty:
        return new_df

    if new_df.empty:
        # Remove issue from existing data (deletion case)
        return existing_df[existing_df[key_column] != issue_key].copy()

    # Remove old records for this issue, add new ones
    filtered = existing_df[existing_df[key_column] != issue_key]
    return pd.concat([filtered, new_df], ignore_index=True)


def _hive_dir(parquet_dir: Path, month_key: str) -> Path:
    """Return the hive partition directory for a given month key."""
    return parquet_dir / f"{HIVE_PARTITION_PREFIX}={month_key}"


def _flat_path(parquet_dir: Path, month_key: str) -> Path:
    """Return the legacy flat parquet path for a given month key."""
    return parquet_dir / f"{month_key}.parquet"


def load_parquet_month(parquet_dir: Path, month_key: str) -> pd.DataFrame | None:
    """Load existing Parquet file for a month, or return None.

    Checks hive layout (``month=YYYY-MM/data.parquet``) first, then falls back
    to the legacy flat layout (``YYYY-MM.parquet``) for backward compatibility
    during the transition period.
    """
    hive_file = _hive_dir(parquet_dir, month_key) / "data.parquet"
    if hive_file.exists():
        try:
            return pd.read_parquet(hive_file)
        except Exception as e:
            logger.warning(f"Failed to read {hive_file}: {e}")
        return None

    # Backward-compat: flat file from before hive migration
    flat_file = _flat_path(parquet_dir, month_key)
    if flat_file.exists():
        try:
            return pd.read_parquet(flat_file)
        except Exception as e:
            logger.warning(f"Failed to read {flat_file}: {e}")
    return None


def save_parquet_month(
    df: pd.DataFrame,
    schema: dict,
    output_dir: Path,
    month_key: str,
) -> Path:
    """Save DataFrame to the hive-partitioned monthly Parquet layout.

    Writes to ``output_dir/month=<month_key>/data.parquet`` with ZSTD
    compression and column statistics enabled.

    If a legacy flat file (``YYYY-MM.parquet``) exists for the same month it is
    removed after the hive write succeeds, completing the per-month migration.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    hive_dir = _hive_dir(output_dir, month_key)
    output_path = hive_dir / "data.parquet"

    if df.empty:
        # Don't write empty files; remove hive dir and legacy flat file if present.
        if hive_dir.exists():
            import shutil

            shutil.rmtree(hive_dir)
            logger.info(f"Removed empty hive dir {hive_dir}")
        flat = _flat_path(output_dir, month_key)
        if flat.exists():
            flat.unlink()
            logger.info(f"Removed legacy flat file {flat}")
        return output_path

    hive_dir.mkdir(parents=True, exist_ok=True)
    table = apply_schema(df, schema)
    pq.write_table(table, output_path, **PARQUET_WRITE_OPTIONS)
    logger.info(f"Saved {len(df)} records to {output_path}")

    # Remove the legacy flat file now that hive layout is written.
    flat = _flat_path(output_dir, month_key)
    if flat.exists():
        flat.unlink()
        logger.info(f"Removed legacy flat file {flat} after hive migration")

    return output_path


def migrate_flat_to_hive(table_dir: Path) -> list[str]:
    """Migrate any remaining flat YYYY-MM.parquet files to hive layout.

    For each ``YYYY-MM.parquet`` found directly under *table_dir*, moves the
    file to ``month=YYYY-MM/data.parquet``.  Skips months that already have a
    hive directory.  Returns the list of month keys that were migrated.

    This is called during ``init_extract`` and after a batch transform run so
    that existing instances transparently transition to the new layout on the
    first webhook or scheduled sync after upgrade.
    """
    migrated: list[str] = []

    for flat_file in sorted(table_dir.glob("*.parquet")):
        month_key = flat_file.stem  # e.g. "2026-01"
        hive_dir = _hive_dir(table_dir, month_key)

        if hive_dir.exists():
            # Already migrated to hive — remove the redundant flat file so the
            # recursive readers don't double-count this month (flat + hive).
            flat_file.unlink()
            logger.info("Removed redundant flat parquet %s (hive already present)", flat_file)
            continue

        hive_dir.mkdir(parents=True, exist_ok=True)
        dest = hive_dir / "data.parquet"
        flat_file.rename(dest)
        logger.info("Migrated flat parquet %s -> %s", flat_file, dest)
        migrated.append(month_key)

    return migrated


def transform_single_issue(
    issue_key: str,
    raw_dir: Path | None = None,
    output_dir: Path | None = None,
    attachments_dir: Path | None = None,
    deleted: bool = False,
) -> bool:
    """
    Transform a single issue and update monthly Parquet files.

    This is called by webhook handler after issue JSON is saved.
    Only updates the month that the issue belongs to.

    Args:
        issue_key: Jira issue key (e.g., "SUPPORT-1234")
        raw_dir: Directory with raw JSON files
        output_dir: Output directory for Parquet files
        attachments_dir: Directory with downloaded attachments
        deleted: If True, remove issue from Parquet (deletion event)

    Returns:
        True if successful, False otherwise
    """
    raw_dir = raw_dir or DEFAULT_RAW_DIR
    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    attachments_dir = attachments_dir or (raw_dir / "attachments")

    # Defense-in-depth: even if a stale/legacy code path bypasses webhook
    # validation, the transform step will refuse a malformed key (issue #83).
    if not is_valid_issue_key(issue_key):
        logger.error(f"Refusing transform for malformed issue key: {issue_key!r}")
        return False
    issues_dir = raw_dir / "issues"
    try:
        json_path = safe_join_under(issues_dir, f"{issue_key}.json")
    except ValueError as e:
        logger.error(f"Path traversal blocked in transform for {issue_key!r}: {e}")
        return False

    if deleted:
        # For deletion, we need to find which month the issue was in
        # Check all monthly files - this is rare so OK to be slower
        logger.info(f"Processing deletion for {issue_key}")
        return _handle_deletion(issue_key, output_dir)

    if not json_path.exists():
        logger.error(f"Issue JSON not found: {json_path}")
        return False

    try:
        # Load raw issue data
        with open(json_path) as f:
            raw_issue = json.load(f)

        # Transform issue
        issue_record = transform_issue(raw_issue)
        issue_record["_raw_file"] = json_path.name

        # Determine month
        month_key = get_month_key(issue_record.get("created_at"))
        logger.info(f"Updating {issue_key} in month {month_key}")

        # Transform related data
        comments_records = transform_comments(raw_issue)
        attachments_records = transform_attachments(raw_issue, attachments_dir)
        changelog_records = transform_changelog(raw_issue)

        # Transform link/remote data outside lock (minimize hold time)
        issuelinks_records = transform_issuelinks(raw_issue)
        remote_links_records = transform_remote_links(raw_issue)

        # Parquet read-modify-write under per-month lock to prevent
        # "last writer wins" race when concurrent webhooks touch the
        # same monthly partition (see issue #205).
        with parquet_month_lock(output_dir, month_key):
            updated_paths = []

            # Issues
            existing_issues = load_parquet_month(output_dir / "issues", month_key)
            updated_issues = upsert_dataframe(existing_issues, [issue_record], "issue_key", issue_key)
            path = save_parquet_month(updated_issues, ISSUES_SCHEMA, output_dir / "issues", month_key)
            updated_paths.append(path)

            # Comments
            existing_comments = load_parquet_month(output_dir / "comments", month_key)
            updated_comments = upsert_dataframe(existing_comments, comments_records, "issue_key", issue_key)
            path = save_parquet_month(updated_comments, COMMENTS_SCHEMA, output_dir / "comments", month_key)
            updated_paths.append(path)

            # Attachments
            existing_attachments = load_parquet_month(output_dir / "attachments", month_key)
            updated_attachments = upsert_dataframe(existing_attachments, attachments_records, "issue_key", issue_key)
            path = save_parquet_month(updated_attachments, ATTACHMENTS_SCHEMA, output_dir / "attachments", month_key)
            updated_paths.append(path)

            # Changelog
            existing_changelog = load_parquet_month(output_dir / "changelog", month_key)
            updated_changelog = upsert_dataframe(existing_changelog, changelog_records, "issue_key", issue_key)
            path = save_parquet_month(updated_changelog, CHANGELOG_SCHEMA, output_dir / "changelog", month_key)
            updated_paths.append(path)

            # Issue links
            existing_issuelinks = load_parquet_month(output_dir / "issuelinks", month_key)
            updated_issuelinks = upsert_dataframe(existing_issuelinks, issuelinks_records, "issue_key", issue_key)
            path = save_parquet_month(updated_issuelinks, ISSUELINKS_SCHEMA, output_dir / "issuelinks", month_key)
            updated_paths.append(path)

            # Remote links
            if remote_links_records is not None:
                existing_remote_links = load_parquet_month(output_dir / "remote_links", month_key)
                updated_remote_links = upsert_dataframe(
                    existing_remote_links, remote_links_records, "issue_key", issue_key
                )
                path = save_parquet_month(
                    updated_remote_links, REMOTE_LINKS_SCHEMA, output_dir / "remote_links", month_key
                )
                updated_paths.append(path)
            else:
                # The writer (save_issue / backfill / backfill_remote_links) skipped
                # the _remote_links overlay due to a Jira fetch failure. Preserve the
                # existing parquet rows for this issue instead of wiping them.
                logger.warning(
                    f"Skipping remote_links upsert for {issue_key}: overlay absent "
                    f"(fetch failure). Existing rows preserved."
                )

        # Update extract.duckdb _meta for all affected tables
        try:
            from .extract_init import update_meta

            extract_dir = output_dir.parent  # output_dir is .../data, parent is .../jira
            for table_name in ["issues", "comments", "attachments", "changelog", "issuelinks", "remote_links"]:
                update_meta(extract_dir, table_name)
        except Exception as meta_err:
            logger.warning(f"Could not update extract.duckdb _meta: {meta_err}")

        logger.info(f"Successfully updated {issue_key} in Parquet files")
        return True

    except Exception as e:
        logger.error(f"Error transforming {issue_key}: {e}", exc_info=True)
        return False


def _handle_deletion(
    issue_key: str,
    output_dir: Path,
) -> bool:
    """Handle issue deletion by removing from all monthly files."""
    found = False

    for table_name, schema in [
        ("issues", ISSUES_SCHEMA),
        ("comments", COMMENTS_SCHEMA),
        ("attachments", ATTACHMENTS_SCHEMA),
        ("changelog", CHANGELOG_SCHEMA),
        ("issuelinks", ISSUELINKS_SCHEMA),
        ("remote_links", REMOTE_LINKS_SCHEMA),
    ]:
        table_dir = output_dir / table_name
        if not table_dir.exists():
            continue

        # Collect all month keys from both hive dirs and legacy flat files.
        month_keys: set[str] = set()
        for hive_subdir in table_dir.glob(f"{HIVE_PARTITION_PREFIX}=*"):
            if hive_subdir.is_dir():
                month_keys.add(hive_subdir.name.split("=", 1)[1])
        for flat_file in table_dir.glob("*.parquet"):
            month_keys.add(flat_file.stem)

        for month_key in sorted(month_keys):
            parquet_file = _hive_dir(table_dir, month_key) / "data.parquet"
            if not parquet_file.exists():
                # Fall back to flat layout for backward compat
                parquet_file = _flat_path(table_dir, month_key)
            if not parquet_file.exists():
                continue
            try:
                with parquet_month_lock(output_dir, month_key):
                    df = pd.read_parquet(parquet_file)
                    if "issue_key" in df.columns and issue_key in df["issue_key"].values:
                        df = df[df["issue_key"] != issue_key]
                        save_parquet_month(df, schema, table_dir, month_key)

                        found = True
                        logger.info(f"Removed {issue_key} from {parquet_file}")
            except Exception as e:
                logger.warning(f"Error checking {parquet_file}: {e}")

    return found


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Incremental Jira transform")
    parser.add_argument("issue_key", help="Jira issue key (e.g., SUPPORT-1234)")
    parser.add_argument("--raw-dir", type=Path, help="Raw JSON directory")
    parser.add_argument("--output-dir", type=Path, help="Output Parquet directory")
    parser.add_argument("--attachments-dir", type=Path, help="Attachments directory")
    parser.add_argument("--deleted", action="store_true", help="Issue was deleted")

    args = parser.parse_args()

    success = transform_single_issue(
        issue_key=args.issue_key,
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        attachments_dir=args.attachments_dir,
        deleted=args.deleted,
    )

    exit(0 if success else 1)
