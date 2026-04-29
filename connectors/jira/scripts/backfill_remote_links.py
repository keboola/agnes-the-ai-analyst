#!/usr/bin/env python3
"""
Jira Remote Links Backfill - Add _remote_links to existing issue JSONs.

One-time migration script that fetches remote links from Jira API
and embeds them into existing issue JSON files. This enables the
Parquet transform to extract remote_links table data.

Usage:
    # On server (uses /opt/data-analyst/.env):
    python -m connectors.jira.scripts.backfill_remote_links

    # With parallel workers:
    python -m connectors.jira.scripts.backfill_remote_links --parallel 4

    # Dry run:
    python -m connectors.jira.scripts.backfill_remote_links --dry-run

Environment variables (loaded from .env):
    JIRA_DOMAIN - Jira Cloud domain
    JIRA_EMAIL - Email for API authentication
    JIRA_API_TOKEN - API token from Atlassian
    JIRA_DATA_DIR - Directory for storing data (default: /data/src_data/raw/jira)
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.logging_config import setup_logging

setup_logging(__name__)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """Load configuration from environment variables."""
    env_paths = [
        Path("/opt/data-analyst/.env"),
        Path.cwd() / ".env",
        Path(__file__).parent.parent / ".env",
    ]
    for env_path in env_paths:
        if env_path.exists():
            load_dotenv(env_path)
            logger.info(f"Loaded environment from {env_path}")
            break

    required = ["JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN"]
    missing = [var for var in required if not os.environ.get(var)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return {
        "domain": os.environ["JIRA_DOMAIN"],
        "email": os.environ["JIRA_EMAIL"],
        "api_token": os.environ["JIRA_API_TOKEN"],
        "data_dir": Path(os.environ.get("JIRA_DATA_DIR", "/data/src_data/raw/jira")),
    }


def fetch_remote_links(base_url: str, auth: tuple[str, str], issue_key: str) -> list[dict]:
    """Fetch remote links for a single issue from Jira API."""
    url = f"{base_url}/issue/{issue_key}/remotelink"

    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                url,
                auth=auth,
                headers={"Accept": "application/json"},
            )

        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            return []
        elif response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning(f"Rate limited, waiting {retry_after}s...")
            time.sleep(retry_after)
            return fetch_remote_links(base_url, auth, issue_key)
        else:
            logger.debug(f"Failed to fetch remote links for {issue_key}: {response.status_code}")
            return []

    except httpx.RequestError as e:
        logger.debug(f"Error fetching remote links for {issue_key}: {e}")
        return []


def process_file(json_path: Path, base_url: str, auth: tuple[str, str]) -> str:
    """
    Process a single issue JSON file.

    Returns: "processed", "skipped", or "failed"
    """
    try:
        with open(json_path) as f:
            data = json.load(f)

        # Skip if already has _remote_links
        if "_remote_links" in data:
            return "skipped"

        issue_key = data.get("key")
        if not issue_key:
            return "failed"

        # Fetch remote links
        remote_links = fetch_remote_links(base_url, auth, issue_key)

        # Embed in data
        data["_remote_links"] = remote_links

        # Atomic write: temp file + replace
        fd, tmp_path = tempfile.mkstemp(dir=str(json_path.parent), suffix=".tmp")
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

        return "processed"

    except Exception as e:
        logger.error(f"Error processing {json_path.name}: {e}")
        return "failed"


def main():
    parser = argparse.ArgumentParser(
        description="Backfill _remote_links into existing Jira issue JSONs",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count files, don't fetch or modify",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Override data directory",
    )

    args = parser.parse_args()

    config = load_config()
    data_dir = args.data_dir or config["data_dir"]
    issues_dir = data_dir / "issues"

    if not issues_dir.exists():
        logger.error(f"Issues directory not found: {issues_dir}")
        sys.exit(1)

    base_url = f"https://{config['domain']}/rest/api/3"
    auth = (config["email"], config["api_token"])

    # Enumerate JSON files
    json_files = list(issues_dir.glob("*.json"))
    total = len(json_files)
    logger.info(f"Found {total} issue JSON files in {issues_dir}")

    if args.dry_run:
        # Count how many already have _remote_links
        already_done = 0
        for jf in json_files:
            try:
                with open(jf) as f:
                    data = json.load(f)
                if "_remote_links" in data:
                    already_done += 1
            except Exception:
                pass
        logger.info(f"Already have _remote_links: {already_done}")
        logger.info(f"Would process: {total - already_done}")
        return

    # Process files
    stats = {"processed": 0, "skipped": 0, "failed": 0}
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {executor.submit(process_file, jf, base_url, auth): jf for jf in json_files}

        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            result = future.result()
            stats[result] += 1

            if done_count % 200 == 0:
                elapsed = time.time() - start_time
                rate = done_count / elapsed if elapsed > 0 else 0
                logger.info(
                    f"Progress: {done_count}/{total} "
                    f"({rate:.1f}/s) - "
                    f"processed: {stats['processed']}, "
                    f"skipped: {stats['skipped']}, "
                    f"failed: {stats['failed']}"
                )

    elapsed = time.time() - start_time

    logger.info("=" * 60)
    logger.info("Remote links backfill completed!")
    logger.info(f"Total files: {total}")
    logger.info(f"Processed: {stats['processed']}")
    logger.info(f"Skipped (already had _remote_links): {stats['skipped']}")
    logger.info(f"Failed: {stats['failed']}")
    logger.info(f"Time: {elapsed:.1f}s")
    logger.info("=" * 60)

    if stats["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
