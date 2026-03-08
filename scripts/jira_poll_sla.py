#!/usr/bin/env python3
"""
Jira SLA Polling - Refresh SLA data and self-heal stale status for open tickets.

Periodic job that finds open issues with SLA data in Parquet, fetches
fresh SLA elapsed_millis + status fields from the Jira API, and updates
raw JSON + Parquet files. This keeps SLA breach tracking accurate for
idle tickets where no webhook fires to refresh the snapshot.

Self-healing: also fetches status/resolution fields so tickets resolved
in Jira (but stale in local data due to missed webhooks) get corrected
automatically on the next poll cycle.

Designed to run as a systemd timer (every 15 min) via jira-sla-poll.timer.

Usage:
    # On server:
    python scripts/jira_poll_sla.py

    # Dry run (count open issues, don't fetch):
    python scripts/jira_poll_sla.py --dry-run

    # Verbose logging:
    python scripts/jira_poll_sla.py --verbose

Environment variables (loaded from .env):
    JIRA_SLA_EMAIL - Email for JSM service account authentication
    JIRA_SLA_API_TOKEN - API token for JSM service account
    JIRA_CLOUD_ID - Atlassian Cloud site ID (for cloud API base URL)
    JIRA_DATA_DIR - Directory for raw Jira data (default: /data/src_data/raw/jira)
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

# Add project root to sys.path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.jira_backfill_sla import (
    SLA_FIELDS,
    has_valid_sla_data,
    load_config,
)
from src.incremental_jira_transform import transform_single_issue
from src.jira_file_lock import issue_json_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Additional fields to fetch for self-healing stale status
STATUS_FIELDS = ["status", "resolution", "resolutiondate", "updated"]


def fetch_sla_and_status(
    base_url: str, auth: tuple[str, str], issue_key: str
) -> dict | None:
    """
    Fetch SLA fields AND status/resolution fields for a single issue.

    Extends the SLA-only fetch to also request status, resolution,
    resolutiondate, and updated - enabling self-healing of stale data.

    Returns dict with all field values, or None on failure.
    """
    all_fields = SLA_FIELDS + STATUS_FIELDS
    url = f"{base_url}/issue/{issue_key}"
    params = {"fields": ",".join(all_fields)}

    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                url,
                auth=auth,
                params=params,
                headers={"Accept": "application/json"},
            )

        if response.status_code == 200:
            return response.json().get("fields", {})
        elif response.status_code == 404:
            logger.debug(f"Issue {issue_key} not found")
            return None
        elif response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning(f"Rate limited, waiting {retry_after}s...")
            time.sleep(retry_after)
            return fetch_sla_and_status(base_url, auth, issue_key)
        else:
            logger.warning(
                f"Failed to fetch SLA+status for {issue_key}: "
                f"{response.status_code} {response.text[:200]}"
            )
            return None

    except httpx.RequestError as e:
        logger.error(f"Request error fetching SLA+status for {issue_key}: {e}")
        return None


def find_open_issues_with_sla(parquet_dir: Path) -> list[str]:
    """
    Read Parquet issues and return keys of open tickets that have SLA data.

    An issue qualifies if:
    - status_category != 'Done' (still open)
    - Has non-null first_response_elapsed_millis OR time_to_resolution_elapsed_millis
    """
    issues_dir = parquet_dir / "issues"
    if not issues_dir.exists():
        logger.error(f"Issues Parquet directory not found: {issues_dir}")
        return []

    parquet_files = sorted(issues_dir.glob("*.parquet"))
    if not parquet_files:
        logger.error(f"No Parquet files found in {issues_dir}")
        return []

    logger.info(f"Reading {len(parquet_files)} Parquet files from {issues_dir}")

    # Read only needed columns for efficiency
    columns = [
        "issue_key",
        "status_category",
        "first_response_elapsed_millis",
        "time_to_resolution_elapsed_millis",
    ]

    dfs = []
    for pf in parquet_files:
        try:
            df = pd.read_parquet(pf, columns=columns)
            dfs.append(df)
        except Exception as e:
            logger.warning(f"Failed to read {pf}: {e}")

    if not dfs:
        return []

    all_issues = pd.concat(dfs, ignore_index=True)
    logger.info(f"Total issues in Parquet: {len(all_issues)}")

    # Filter: open issues with SLA data
    open_with_sla = all_issues[
        (all_issues["status_category"] != "Done")
        & (
            all_issues["first_response_elapsed_millis"].notna()
            | all_issues["time_to_resolution_elapsed_millis"].notna()
        )
    ]

    issue_keys = open_with_sla["issue_key"].tolist()
    logger.info(f"Open issues with SLA data: {len(issue_keys)}")
    return issue_keys


def update_issue_sla(
    issue_key: str,
    raw_dir: Path,
    base_url: str,
    auth: tuple[str, str],
) -> str:
    """
    Fetch fresh SLA + status data for a single issue, update raw JSON,
    and re-transform to Parquet.

    Self-healing: if the API returns a resolved status for an issue that
    was "open" in Parquet, the status fields in JSON are updated so the
    next Parquet transform reflects the correct state.

    The entire read-modify-write + transform is wrapped in an advisory
    file lock to prevent races with the webhook handler.

    Returns: "updated", "skipped", "healed", or "failed"
    """
    issues_dir = raw_dir / "issues"
    json_path = issues_dir / f"{issue_key}.json"
    if not json_path.exists():
        logger.warning(f"Raw JSON not found for {issue_key}, skipping")
        return "skipped"

    # Fetch fresh SLA + status fields from API
    api_data = fetch_sla_and_status(base_url, auth, issue_key)
    if api_data is None:
        logger.warning(f"Failed to fetch SLA+status for {issue_key}")
        return "failed"

    # Check if any SLA field has valid data
    has_sla_data = any(has_valid_sla_data(api_data.get(f)) for f in SLA_FIELDS)

    # Check if status indicates resolution (self-healing)
    api_status = api_data.get("status")
    api_status_category = None
    if isinstance(api_status, dict):
        status_cat = api_status.get("statusCategory")
        if isinstance(status_cat, dict):
            api_status_category = status_cat.get("name")

    is_healed = api_status_category == "Done"

    if not has_sla_data and not is_healed:
        logger.debug(f"No SLA data and not resolved for {issue_key}")
        return "skipped"

    # Lock, read-modify-write, and transform atomically
    with issue_json_lock(issues_dir, issue_key):
        # Load existing JSON
        try:
            with open(json_path) as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read {json_path}: {e}")
            return "failed"

        if "fields" not in data:
            data["fields"] = {}

        # Update SLA fields
        for sla_field in SLA_FIELDS:
            if sla_field in api_data:
                data["fields"][sla_field] = api_data[sla_field]

        # Update status fields (self-healing)
        if api_status is not None:
            data["fields"]["status"] = api_data["status"]
        if api_data.get("resolution") is not None:
            data["fields"]["resolution"] = api_data["resolution"]
        if api_data.get("resolutiondate") is not None:
            data["fields"]["resolutiondate"] = api_data["resolutiondate"]
        if api_data.get("updated") is not None:
            data["fields"]["updated"] = api_data["updated"]

        if is_healed:
            logger.info(f"Self-healing: {issue_key} is resolved in Jira")

        # Atomic write: temp file + replace
        fd, tmp_path = tempfile.mkstemp(dir=str(json_path.parent), suffix=".tmp")
        os.fchmod(fd, 0o660)  # Restore group rw so www-data/deploy can access via ACL
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp_path, str(json_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # Re-transform to Parquet (inside lock to prevent stale reads)
        success = transform_single_issue(issue_key=issue_key)
        if not success:
            logger.error(f"Failed to transform {issue_key} after SLA update")
            return "failed"

    return "healed" if is_healed else "updated"


def main():
    parser = argparse.ArgumentParser(
        description="Poll open Jira tickets for fresh SLA data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count open issues with SLA data, don't fetch or modify",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config()
    raw_dir = config["data_dir"]
    parquet_dir = Path(os.environ.get(
        "JIRA_PARQUET_DIR", "/data/src_data/parquet/jira"
    ))
    base_url = config["base_url"]
    auth = (config["email"], config["api_token"])

    # Find open issues with SLA data
    open_issues = find_open_issues_with_sla(parquet_dir)

    if not open_issues:
        logger.info("No open issues with SLA data found")
        return

    if args.dry_run:
        logger.info(f"Dry run: would poll {len(open_issues)} open issues:")
        for key in sorted(open_issues):
            logger.info(f"  {key}")
        return

    # Process each open issue
    stats = {"updated": 0, "skipped": 0, "failed": 0, "healed": 0}
    start_time = time.time()

    for i, issue_key in enumerate(sorted(open_issues), 1):
        logger.info(f"[{i}/{len(open_issues)}] Polling {issue_key}...")

        result = update_issue_sla(issue_key, raw_dir, base_url, auth)
        stats[result] += 1

        # Brief pause between API calls to be respectful
        time.sleep(0.5)

    elapsed = time.time() - start_time

    logger.info("=" * 60)
    logger.info("SLA polling completed!")
    logger.info(f"Open issues polled: {len(open_issues)}")
    logger.info(f"Updated (SLA only): {stats['updated']}")
    logger.info(f"Healed (status corrected): {stats['healed']}")
    logger.info(f"Skipped: {stats['skipped']}")
    logger.info(f"Failed: {stats['failed']}")
    logger.info(f"Time: {elapsed:.1f}s")
    logger.info("=" * 60)

    if stats["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
