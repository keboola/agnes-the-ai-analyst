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
    python -m connectors.jira.scripts.poll_sla

    # Dry run (count open issues, don't fetch):
    python -m connectors.jira.scripts.poll_sla --dry-run

    # Verbose logging:
    python -m connectors.jira.scripts.poll_sla --verbose

Environment variables (loaded from .env):
    JIRA_DOMAIN - Atlassian site host (e.g. your-org.atlassian.net)
    JIRA_EMAIL - Email for API authentication
    JIRA_API_TOKEN - Primary API token (account needs a JSM Agent licence)
    JIRA_CLOUD_ID - Optional; set only for a scoped token (gateway base URL)
    JIRA_REFRESH_FIELDS - field ids to refresh (field_id or field_id:column, comma-separated)
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

# Add project root to sys.path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.jira.scripts.backfill_sla import (  # noqa: E402
    configured_field_ids,
    load_config,
)
from connectors.jira.incremental_transform import transform_single_issue  # noqa: E402
from connectors.jira.file_lock import issue_json_lock  # noqa: E402

from app.logging_config import setup_logging  # noqa: E402

setup_logging(__name__)
logger = logging.getLogger(__name__)

# Additional fields to fetch for self-healing stale status
STATUS_FIELDS = ["status", "resolution", "resolutiondate", "updated"]


def fetch_sla_and_status(base_url: str, auth: tuple[str, str], issue_key: str) -> dict | None:
    """
    Fetch SLA fields AND status/resolution fields for a single issue.

    Extends the SLA-only fetch to also request status, resolution,
    resolutiondate, and updated - enabling self-healing of stale data.

    Returns dict with all field values, or None on failure.
    """
    all_fields = configured_field_ids() + STATUS_FIELDS
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
            logger.warning(f"Failed to fetch SLA+status for {issue_key}: {response.status_code} {response.text[:200]}")
            return None

    except httpx.RequestError as e:
        logger.error(f"Request error fetching SLA+status for {issue_key}: {e}")
        return None


def find_open_issues(parquet_dir: Path) -> list[str]:
    """
    Read Parquet issues and return keys of open tickets (status_category != 'Done').

    Open tickets are the ones whose field values can still change, so they are the
    ones worth re-fetching each poll cycle.
    """
    issues_dir = parquet_dir / "issues"
    if not issues_dir.exists():
        logger.error(f"Issues Parquet directory not found: {issues_dir}")
        return []

    # Recursive: matches both flat (<table>/<YYYY-MM>.parquet) and hive
    # (<table>/month=<YYYY-MM>/data.parquet) Jira parquet layouts.
    parquet_files = sorted(issues_dir.rglob("*.parquet"))
    if not parquet_files:
        logger.error(f"No Parquet files found in {issues_dir}")
        return []

    logger.info(f"Reading {len(parquet_files)} Parquet files from {issues_dir}")

    columns = ["issue_key", "status_category"]
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

    open_issues = all_issues[all_issues["status_category"] != "Done"]
    issue_keys = open_issues["issue_key"].tolist()
    logger.info(f"Open issues: {len(issue_keys)}")
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

    # Did any configured field come back with a value to refresh?
    has_data = any(api_data.get(f) is not None for f in configured_field_ids())

    # Check if status indicates resolution (self-healing)
    api_status = api_data.get("status")
    api_status_category = None
    if isinstance(api_status, dict):
        status_cat = api_status.get("statusCategory")
        if isinstance(status_cat, dict):
            api_status_category = status_cat.get("name")

    is_healed = api_status_category == "Done"

    if not has_data and not is_healed:
        logger.debug(f"No fresh field data and not resolved for {issue_key}")
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

        # Update the configured fields
        for field_id in configured_field_ids():
            if field_id in api_data:
                data["fields"][field_id] = api_data[field_id]

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


def run(dry_run: bool = False, verbose: bool = False) -> dict:
    """Poll open Jira tickets for fresh SLA data and self-heal stale status.

    Programmatic entry point for the scheduler endpoint
    ``/api/admin/run-jira-sla-poll``. Mirrors what ``main()`` does as a
    CLI script, but returns a stats dict instead of calling ``sys.exit``.

    Returns a dict with keys: ``open_issues``, ``updated``, ``healed``,
    ``skipped``, ``failed``, ``elapsed_sec``, ``dry_run``. Raises
    ``ValueError`` (from ``load_config``) when required ``JIRA_*`` env
    vars are missing — callers handle that as "Jira not configured" and
    skip the run.
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config()
    raw_dir = config["data_dir"]
    parquet_dir = Path(os.environ.get("JIRA_PARQUET_DIR", "/data/src_data/parquet/jira"))
    base_url = config["base_url"]
    auth = (config["email"], config["api_token"])

    open_issues = find_open_issues(parquet_dir)

    if not open_issues:
        logger.info("No open issues found")
        return {
            "open_issues": 0,
            "updated": 0,
            "healed": 0,
            "skipped": 0,
            "failed": 0,
            "elapsed_sec": 0.0,
            "dry_run": dry_run,
        }

    if dry_run:
        logger.info(f"Dry run: would poll {len(open_issues)} open issues")
        return {
            "open_issues": len(open_issues),
            "updated": 0,
            "healed": 0,
            "skipped": 0,
            "failed": 0,
            "elapsed_sec": 0.0,
            "dry_run": True,
        }

    stats = {"updated": 0, "skipped": 0, "failed": 0, "healed": 0}
    start_time = time.time()

    for i, issue_key in enumerate(sorted(open_issues), 1):
        logger.info(f"[{i}/{len(open_issues)}] Polling {issue_key}...")
        result = update_issue_sla(issue_key, raw_dir, base_url, auth)
        stats[result] += 1
        time.sleep(0.5)  # gentle on the Jira API

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

    return {
        "open_issues": len(open_issues),
        "updated": stats["updated"],
        "healed": stats["healed"],
        "skipped": stats["skipped"],
        "failed": stats["failed"],
        "elapsed_sec": elapsed,
        "dry_run": False,
    }


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

    stats = run(dry_run=args.dry_run, verbose=args.verbose)
    if stats["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
