#!/usr/bin/env python3
"""
Jira Backfill Script - Download all historical Jira issues.

Downloads all issues from Jira using JQL search with pagination.
Reuses the webapp's JiraService for consistent data handling.

Usage:
    # On server (uses /opt/data-analyst/.env):
    python -m connectors.jira.scripts.backfill

    # With custom settings:
    python -m connectors.jira.scripts.backfill --jql "project = MY_PROJECT AND created >= 2025-01-01"

    # Skip already downloaded issues:
    python -m connectors.jira.scripts.backfill --skip-existing

    # Dry run (show what would be downloaded):
    python -m connectors.jira.scripts.backfill --dry-run

Environment variables (loaded from .env or set manually):
    JIRA_DOMAIN - Jira Cloud domain (e.g., your-org.atlassian.net)
    JIRA_EMAIL - Email for API authentication
    JIRA_API_TOKEN - API token from Atlassian
    JIRA_DATA_DIR - Directory for storing data (default: /data/src_data/raw/jira)
"""

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

import httpx
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class Config:
    """Configuration loaded from environment."""

    jira_domain: str
    jira_email: str
    jira_api_token: str
    data_dir: Path

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        # Try to load .env file from common locations
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

        # Validate required variables
        required = ["JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN"]
        missing = [var for var in required if not os.environ.get(var)]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            jira_domain=os.environ["JIRA_DOMAIN"],
            jira_email=os.environ["JIRA_EMAIL"],
            jira_api_token=os.environ["JIRA_API_TOKEN"],
            data_dir=Path(os.environ.get("JIRA_DATA_DIR", "/data/src_data/raw/jira")),
        )


class JiraBackfill:
    """Backfill handler for downloading all Jira issues."""

    # Jira API limits
    MAX_RESULTS_PER_PAGE = 100
    MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024  # 50 MB

    def __init__(self, config: Config):
        self.config = config
        self.base_url = f"https://{config.jira_domain}/rest/api/3"
        self.auth = (config.jira_email, config.jira_api_token)
        self.issues_dir = config.data_dir / "issues"
        self.attachments_dir = config.data_dir / "attachments"

        # Ensure directories exist
        self.issues_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)

        # Statistics
        self.stats = {
            "searched": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "attachments": 0,
        }

    def search_issues(self, jql: str, next_page_token: str | None = None) -> dict:
        """
        Search for issues using JQL (new /search/jql endpoint).

        Args:
            jql: JQL query string
            next_page_token: Pagination token from previous response

        Returns:
            Search results dict with issues and nextPageToken
        """
        url = f"{self.base_url}/search/jql"
        payload = {
            "jql": jql,
            "maxResults": self.MAX_RESULTS_PER_PAGE,
            "fields": ["key"],  # Only need keys, we'll fetch full data separately
        }

        if next_page_token:
            payload["nextPageToken"] = next_page_token

        with httpx.Client(timeout=60) as client:
            response = client.post(
                url,
                auth=self.auth,
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        if response.status_code != 200:
            raise RuntimeError(f"Search failed: {response.status_code} - {response.text[:200]}")

        return response.json()

    def iter_issue_keys(self, jql: str) -> Iterator[str]:
        """
        Iterate over all issue keys matching JQL query.

        Handles pagination automatically using nextPageToken.

        Args:
            jql: JQL query string

        Yields:
            Issue keys (e.g., "PROJ-15190")
        """
        next_page_token = None
        total_fetched = 0
        first_page = True

        while True:
            result = self.search_issues(jql, next_page_token)

            if first_page:
                # Note: new API doesn't return total, we discover it as we paginate
                logger.info(f"Starting search with JQL: {jql}")
                first_page = False

            issues = result.get("issues", [])
            if not issues:
                break

            for issue in issues:
                yield issue["key"]

            total_fetched += len(issues)
            self.stats["searched"] = total_fetched

            # Progress logging
            if total_fetched % 500 == 0:
                logger.info(f"Enumerated {total_fetched} issues...")

            # Check for next page
            next_page_token = result.get("nextPageToken")
            if not next_page_token:
                break

            # Respect rate limits
            time.sleep(0.1)

        logger.info(f"Found {total_fetched} issues total")

    def fetch_issue(self, issue_key: str) -> dict | None:
        """
        Fetch complete issue data from Jira.

        Args:
            issue_key: Issue key (e.g., "PROJ-123")

        Returns:
            Issue data dict or None if fetch failed
        """
        url = f"{self.base_url}/issue/{issue_key}"
        params = {
            "expand": "renderedFields,changelog",
            "fields": "*all",
        }

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=self.auth,
                    params=params,
                    headers={"Accept": "application/json"},
                )

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.warning(f"Issue {issue_key} not found")
                return None
            elif response.status_code == 429:
                # Rate limited - wait and retry
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning(f"Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after)
                return self.fetch_issue(issue_key)  # Retry
            else:
                logger.error(f"Failed to fetch {issue_key}: {response.status_code}")
                return None

        except httpx.RequestError as e:
            logger.error(f"Request error fetching {issue_key}: {e}")
            return None

    def fetch_remote_links(self, issue_key: str) -> list[dict]:
        """
        Fetch remote links for an issue from Jira.

        Args:
            issue_key: Issue key (e.g., "PROJ-123")

        Returns:
            List of remote link dicts, empty list on failure
        """
        url = f"{self.base_url}/issue/{issue_key}/remotelink"

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=self.auth,
                    headers={"Accept": "application/json"},
                )

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                return []
            elif response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning(f"Rate limited on remote links, waiting {retry_after}s...")
                time.sleep(retry_after)
                return self.fetch_remote_links(issue_key)
            else:
                logger.debug(f"Failed to fetch remote links for {issue_key}: {response.status_code}")
                return []

        except httpx.RequestError as e:
            logger.debug(f"Error fetching remote links for {issue_key}: {e}")
            return []

    def save_issue(self, issue_data: dict) -> Path | None:
        """
        Save issue data to JSON file.

        Args:
            issue_data: Complete issue data from Jira API

        Returns:
            Path to saved file or None if save failed
        """
        issue_key = issue_data.get("key")
        if not issue_key:
            return None

        # Add sync metadata
        issue_data["_synced_at"] = datetime.utcnow().isoformat()

        file_path = self.issues_dir / f"{issue_key}.json"

        try:
            with open(file_path, "w") as f:
                json.dump(issue_data, f, indent=2, default=str)
            return file_path
        except Exception as e:
            logger.error(f"Failed to save {issue_key}: {e}")
            return None

    def download_attachment(self, attachment: dict, issue_key: str) -> Path | None:
        """
        Download a single attachment.

        Args:
            attachment: Attachment metadata from Jira
            issue_key: Issue key for organizing files

        Returns:
            Path to downloaded file or None if failed
        """
        content_url = attachment.get("content")
        filename = attachment.get("filename", "unknown")
        size = attachment.get("size", 0)
        attachment_id = attachment.get("id", "unknown")

        if not content_url:
            return None

        # Skip large attachments
        if size > self.MAX_ATTACHMENT_SIZE:
            logger.debug(f"Skipping large attachment {filename} ({size} bytes)")
            return None

        # Create issue-specific directory
        issue_attachments_dir = self.attachments_dir / issue_key
        issue_attachments_dir.mkdir(parents=True, exist_ok=True)

        safe_filename = f"{attachment_id}_{filename}"
        file_path = issue_attachments_dir / safe_filename

        # Skip if already downloaded
        if file_path.exists():
            return file_path

        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                response = client.get(content_url, auth=self.auth)

            if response.status_code == 200:
                with open(file_path, "wb") as f:
                    f.write(response.content)
                return file_path
            else:
                logger.debug(f"Failed to download {filename}: {response.status_code}")
                return None

        except httpx.RequestError as e:
            logger.debug(f"Error downloading {filename}: {e}")
            return None

    def download_issue_attachments(self, issue_data: dict) -> int:
        """
        Download all attachments for an issue.

        Args:
            issue_data: Complete issue data

        Returns:
            Number of attachments downloaded
        """
        issue_key = issue_data.get("key", "unknown")
        attachments = issue_data.get("fields", {}).get("attachment", [])

        downloaded = 0
        for attachment in attachments:
            if self.download_attachment(attachment, issue_key):
                downloaded += 1

        return downloaded

    def process_issue(self, issue_key: str, skip_existing: bool = True) -> bool:
        """
        Fetch and save a single issue with attachments.

        Args:
            issue_key: Issue key to process
            skip_existing: Skip if JSON already exists

        Returns:
            True if successful, False otherwise
        """
        # Check if already downloaded
        json_path = self.issues_dir / f"{issue_key}.json"
        if skip_existing and json_path.exists():
            self.stats["skipped"] += 1
            return True

        # Fetch issue
        issue_data = self.fetch_issue(issue_key)
        if not issue_data:
            self.stats["failed"] += 1
            return False

        # Fetch and embed remote links for Parquet transform
        issue_data["_remote_links"] = self.fetch_remote_links(issue_key)

        # Save JSON
        if not self.save_issue(issue_data):
            self.stats["failed"] += 1
            return False

        # Download attachments
        num_attachments = self.download_issue_attachments(issue_data)
        self.stats["attachments"] += num_attachments
        self.stats["downloaded"] += 1

        return True

    def run(
        self,
        jql: str = "ORDER BY created ASC",
        skip_existing: bool = True,
        dry_run: bool = False,
        parallel: int = 4,
    ) -> dict:
        """
        Run the backfill process.

        Args:
            jql: JQL query for selecting issues
            skip_existing: Skip issues that already have JSON files
            dry_run: Only enumerate issues, don't download
            parallel: Number of parallel download threads

        Returns:
            Statistics dict
        """
        logger.info(f"Starting Jira backfill")
        logger.info(f"JQL: {jql}")
        logger.info(f"Skip existing: {skip_existing}")
        logger.info(f"Dry run: {dry_run}")
        logger.info(f"Data directory: {self.config.data_dir}")

        start_time = time.time()

        # Collect all issue keys first
        issue_keys = list(self.iter_issue_keys(jql))
        total_issues = len(issue_keys)

        logger.info(f"Total issues to process: {total_issues}")

        if dry_run:
            logger.info("Dry run mode - not downloading any data")
            # Count existing
            existing = sum(1 for k in issue_keys if (self.issues_dir / f"{k}.json").exists())
            logger.info(f"Already downloaded: {existing}")
            logger.info(f"Would download: {total_issues - existing}")
            return {"total": total_issues, "existing": existing}

        # Process issues in parallel
        processed = 0
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            # Submit all tasks
            futures = {
                executor.submit(self.process_issue, key, skip_existing): key
                for key in issue_keys
            }

            # Process as completed
            for future in as_completed(futures):
                issue_key = futures[future]
                processed += 1

                try:
                    success = future.result()
                except Exception as e:
                    logger.error(f"Error processing {issue_key}: {e}")
                    self.stats["failed"] += 1

                # Progress logging
                if processed % 100 == 0:
                    elapsed = time.time() - start_time
                    rate = processed / elapsed if elapsed > 0 else 0
                    logger.info(
                        f"Progress: {processed}/{total_issues} "
                        f"({rate:.1f}/s) - "
                        f"downloaded: {self.stats['downloaded']}, "
                        f"skipped: {self.stats['skipped']}, "
                        f"failed: {self.stats['failed']}"
                    )

        elapsed = time.time() - start_time

        # Final summary
        logger.info("=" * 60)
        logger.info("Backfill completed!")
        logger.info(f"Total issues: {total_issues}")
        logger.info(f"Downloaded: {self.stats['downloaded']}")
        logger.info(f"Skipped (existing): {self.stats['skipped']}")
        logger.info(f"Failed: {self.stats['failed']}")
        logger.info(f"Attachments: {self.stats['attachments']}")
        logger.info(f"Time: {elapsed:.1f}s ({total_issues/elapsed:.1f} issues/s)")
        logger.info("=" * 60)

        return self.stats


def main():
    parser = argparse.ArgumentParser(
        description="Download all Jira issues",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--jql",
        default="ORDER BY created ASC",
        help="JQL query for selecting issues (e.g., 'project = \"My Project\" ORDER BY created ASC')",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Skip issues that already have JSON files (default: True)",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_false",
        dest="skip_existing",
        help="Re-download all issues even if they exist",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only count issues, don't download",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=4,
        help="Number of parallel download threads (default: 4)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Override data directory",
    )
    parser.add_argument(
        "--issue-keys",
        help="Comma-separated list of specific issue keys to backfill (e.g., PROJ-123,PROJ-456)",
    )

    args = parser.parse_args()

    try:
        config = Config.from_env()

        # Override data dir if specified
        if args.data_dir:
            config.data_dir = args.data_dir

        backfill = JiraBackfill(config)

        # Targeted backfill mode (specific issue keys)
        if args.issue_keys:
            issue_keys = [key.strip() for key in args.issue_keys.split(",")]
            logger.info(f"Targeted backfill mode: {len(issue_keys)} issues")

            if args.dry_run:
                logger.info("Dry run mode - not downloading any data")
                existing = sum(
                    1 for k in issue_keys
                    if (backfill.issues_dir / f"{k}.json").exists()
                )
                logger.info(f"Already downloaded: {existing}")
                logger.info(f"Would download: {len(issue_keys) - existing}")
                sys.exit(0)

            # Process each issue
            from concurrent.futures import ThreadPoolExecutor, as_completed

            start_time = time.time()
            processed = 0

            with ThreadPoolExecutor(max_workers=args.parallel) as executor:
                futures = {
                    executor.submit(
                        backfill.process_issue,
                        key,
                        args.skip_existing
                    ): key
                    for key in issue_keys
                }

                for future in as_completed(futures):
                    issue_key = futures[future]
                    processed += 1

                    try:
                        success = future.result()
                    except Exception as e:
                        logger.error(f"Error processing {issue_key}: {e}")
                        backfill.stats["failed"] += 1

                    if processed % 10 == 0:
                        logger.info(
                            f"Progress: {processed}/{len(issue_keys)} - "
                            f"downloaded: {backfill.stats['downloaded']}, "
                            f"skipped: {backfill.stats['skipped']}, "
                            f"failed: {backfill.stats['failed']}"
                        )

            elapsed = time.time() - start_time

            # Summary for targeted mode
            logger.info("=" * 60)
            logger.info("Targeted backfill completed!")
            logger.info(f"Total issues: {len(issue_keys)}")
            logger.info(f"Downloaded: {backfill.stats['downloaded']}")
            logger.info(f"Skipped (existing): {backfill.stats['skipped']}")
            logger.info(f"Failed: {backfill.stats['failed']}")
            logger.info(f"Attachments: {backfill.stats['attachments']}")
            logger.info(f"Time: {elapsed:.1f}s")
            logger.info("=" * 60)

            stats = backfill.stats

        # Standard JQL search mode
        else:
            stats = backfill.run(
                jql=args.jql,
                skip_existing=args.skip_existing,
                dry_run=args.dry_run,
                parallel=args.parallel,
            )

        # Exit with error if any failed
        if stats.get("failed", 0) > 0:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Backfill failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
