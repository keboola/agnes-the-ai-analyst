#!/usr/bin/env python3
"""
Jira Backfill Script - Download all historical Jira issues.

Downloads all issues from Jira using JQL search with pagination.
Reuses the webapp's JiraService for consistent data handling.

Usage:
    # On server (loads .env from <install-dir>/.env or the current directory):
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx
from dotenv import load_dotenv

from app.logging_config import setup_logging
from connectors.jira.service import (
    JiraFetchError,
    complete_issue_comments,
    sweep_stale_attachment_staging,
)

setup_logging(__name__)
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
        # Try to load .env file from common locations.
        # Customer-specific install paths (e.g. /opt/<deployment>/.env) can be
        # injected via the AGNES_ENV_FILE env var without editing this list.
        env_paths = [
            Path(os.environ["AGNES_ENV_FILE"]) if os.environ.get("AGNES_ENV_FILE") else None,
            Path.cwd() / ".env",
            Path(__file__).parent.parent / ".env",
        ]
        env_paths = [p for p in env_paths if p is not None]
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


def _incomplete_marker_path(json_path: Path) -> Path:
    """Sidecar marker path for an issue JSON's ``_comments_incomplete`` state.

    A dedicated file next to the JSON — rather than a key inside it — so
    ``--skip-existing`` can answer "does this issue need a re-fetch?" with a
    single ``stat()``, the same cost as the pre-pagination ``json_path.exists()``
    check it replaces. ``_comments_incomplete`` is added to the dict well
    after ``fields`` (often the largest part of a ``fields=*all`` payload),
    so it sits late in the file — a bounded read from the front would
    routinely miss it, and parsing the whole file to find it is exactly the
    six-figure-issue-count cost this sidecar avoids (Devin Review on #1283).
    """
    return json_path.with_suffix(json_path.suffix + ".incomplete")


def _sync_incomplete_marker(json_path: Path, issue_data: dict) -> None:
    """Create/remove the sidecar marker to match ``issue_data``'s current
    ``_comments_incomplete`` state. Called right after writing *json_path*.
    """
    marker_path = _incomplete_marker_path(json_path)
    if issue_data.get("_comments_incomplete") is True:
        marker_path.touch(exist_ok=True)
    else:
        marker_path.unlink(missing_ok=True)


def _needs_refetch(json_path: Path) -> bool:
    """Is an already-downloaded issue JSON one that ``--skip-existing`` must NOT skip?

    True when the sidecar ``.incomplete`` marker exists next to *json_path*
    (comment pagination failed mid-fetch on the write that produced it — see
    ``_sync_incomplete_marker``) — a single ``stat()``, deliberately NOT a
    ``json.load()`` of the JSON itself: at six-figure issue counts, with
    ``fields=*all`` payloads running hundreds of KB each, opening and parsing
    every existing file just to skip it was tens of GB of I/O on every
    ``--skip-existing`` run (Devin Review on #1283).

    Also true when the JSON itself looks truncated/corrupt — cheaply, via
    the last byte rather than a full parse: ``json.dump`` of a dict never
    ends in anything but ``}``, so a file that doesn't is evidence of a
    crashed/partial write, not a completed download.
    """
    marker_path = _incomplete_marker_path(json_path)
    if marker_path.exists():
        logger.info(f"Re-fetching {json_path.name}: stored comments are incomplete")
        return True

    try:
        size = json_path.stat().st_size
        if size == 0:
            logger.warning(f"Re-fetching {json_path.name}: existing JSON is empty")
            return True
        with open(json_path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            last_byte = f.read(1)
    except OSError as e:
        logger.warning(f"Re-fetching {json_path.name}: existing JSON is unreadable ({e})")
        return True
    if last_byte != b"}":
        logger.warning(f"Re-fetching {json_path.name}: existing JSON looks truncated")
        return True
    return False


def _count_already_downloaded(issues_dir: Path, issue_keys: list[str]) -> int:
    """How many of *issue_keys* already have a JSON that ``--skip-existing``
    would actually skip — the same decision ``process_issue`` makes (existence
    AND ``not _needs_refetch(...)``), not a bare ``.exists()``. Shared by both
    ``--dry-run`` paths (JQL search mode and targeted-keys mode) so dry-run's
    "already downloaded" count doesn't under-report what a real run would
    re-fetch (Devin Review on #1283).
    """
    existing = 0
    for key in issue_keys:
        json_path = issues_dir / f"{key}.json"
        if json_path.exists() and not _needs_refetch(json_path):
            existing += 1
    return existing


class JiraBackfill:
    """Backfill handler for downloading all Jira issues."""

    # Jira API limits
    MAX_RESULTS_PER_PAGE = 100
    MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024  # 50 MB
    # Bounded retry for a rate-limited issue fetch, mirroring the comment-
    # pagination retry in complete_issue_comments (service.py). Before this
    # PR the 429 branch lived AFTER the `with httpx.Client(...)` block; this
    # PR moved the 200 branch inside it (to hand `client` to
    # complete_issue_comments) and left the 429 branch's recursive retry
    # inside too, so N consecutive 429s held N open httpx.Client instances
    # plus N stack frames — unbounded (Devin Review on #1283).
    ISSUE_FETCH_RATE_LIMIT_RETRIES = 5

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

        A 429 is retried in a bounded loop (up to ISSUE_FETCH_RATE_LIMIT_RETRIES
        times): only the successful (200) response is handled while `client`
        is open — the retry's `time.sleep()` runs OUTSIDE it, after the
        client (and its connection) is closed, so consecutive 429s don't
        hold multiple open httpx.Client instances (Devin Review on #1283).

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

        rate_limit_retries = 0
        while True:
            try:
                with httpx.Client(timeout=30) as client:
                    response = client.get(
                        url,
                        auth=self.auth,
                        params=params,
                        headers={"Accept": "application/json"},
                    )

                    if response.status_code == 200:
                        issue_data = response.json()
                        complete_issue_comments(issue_data, self.base_url, self.auth, client)
                        return issue_data
            except httpx.RequestError as e:
                logger.error(f"Request error fetching {issue_key}: {e}")
                return None

            if response.status_code == 404:
                logger.warning(f"Issue {issue_key} not found")
                return None

            if response.status_code == 429:
                if rate_limit_retries >= self.ISSUE_FETCH_RATE_LIMIT_RETRIES:
                    logger.error(
                        f"Failed to fetch {issue_key}: still rate limited after "
                        f"{rate_limit_retries} retries — giving up"
                    )
                    return None
                retry_after = int(response.headers.get("Retry-After", 60))
                rate_limit_retries += 1
                logger.warning(
                    f"Rate limited fetching {issue_key}, waiting {retry_after}s "
                    f"(retry {rate_limit_retries}/{self.ISSUE_FETCH_RATE_LIMIT_RETRIES})..."
                )
                time.sleep(retry_after)
                continue

            logger.error(f"Failed to fetch {issue_key}: {response.status_code}")
            return None

    def fetch_remote_links(self, issue_key: str) -> list[dict]:
        """
        Fetch remote links for an issue from Jira.

        Mirrors connectors.jira.service.JiraService.fetch_remote_links —
        raises JiraFetchError on auth (401/403) or server (5xx) failure so
        the caller can skip the overlay rather than wipe existing parquet
        rows. 429 rate-limit retries are kept as-is (legitimate transient).
        """
        url = f"{self.base_url}/issue/{issue_key}/remotelink"

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=self.auth,
                    headers={"Accept": "application/json"},
                )
        except httpx.RequestError as e:
            raise JiraFetchError(f"Backfill remote-links fetch for {issue_key} failed: connection — {e}") from e

        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return []
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning(f"Rate limited on remote links, waiting {retry_after}s...")
            time.sleep(retry_after)
            return self.fetch_remote_links(issue_key)
        if response.status_code in (401, 403):
            raise JiraFetchError(
                f"Backfill remote-links fetch for {issue_key} failed: auth error "
                f"({response.status_code}) — token may be expired/revoked"
            )
        if response.status_code >= 500:
            raise JiraFetchError(
                f"Backfill remote-links fetch for {issue_key} failed: server error ({response.status_code})"
            )
        raise JiraFetchError(
            f"Backfill remote-links fetch for {issue_key} failed: unexpected status {response.status_code}"
        )

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
        issue_data["_synced_at"] = datetime.now(timezone.utc).isoformat()

        file_path = self.issues_dir / f"{issue_key}.json"

        try:
            with open(file_path, "w") as f:
                json.dump(issue_data, f, indent=2, default=str)
            # Keep the sidecar marker in sync with this write's incomplete
            # state (set it when _comments_incomplete is True, clear it
            # otherwise) so _needs_refetch can answer with a stat instead of
            # parsing the JSON body (Devin Review on #1283).
            _sync_incomplete_marker(file_path, issue_data)
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
        sweep_stale_attachment_staging(issue_attachments_dir)

        safe_filename = f"{attachment_id}_{filename}"
        file_path = issue_attachments_dir / safe_filename

        # Skip if already downloaded AND complete — a short file (worker
        # SIGKILLed mid-write by the pre-atomic writer) must be re-fetched,
        # or the download endpoint serves the truncated bytes forever
        # (Devin on #1297).
        if file_path.exists() and (not size or file_path.stat().st_size == size):
            return file_path

        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                response = client.get(content_url, auth=self.auth)

            if response.status_code == 200:
                # Publish atomically (per-process temp + os.replace), mirroring
                # JiraService.download_attachment: the attachment download
                # endpoint serves this very tree, and a webhook-driven
                # incremental transform in another process can catalogue this
                # exact path mid-backfill — a reader must never fstat a
                # partially written file.
                # Bounded temp name: appending to the full name could push a
                # near-NAME_MAX (255-byte) filename over the limit and make a
                # previously-storable attachment fail to save. 40 codepoints
                # (<=160 UTF-8 bytes) keeps the total well under NAME_MAX and
                # stays unique: the name starts with the attachment id.
                # pid alone is not unique enough: the backfill downloads under a
                # thread pool, so two workers on the same attachment would share
                # the staging name — one os.replace() could publish the other's
                # half-written bytes (Devin on #1297).
                tmp_path = file_path.with_name(f".tmp-{os.getpid()}-{os.urandom(4).hex()}-{file_path.name[:32]}")
                try:
                    with open(tmp_path, "wb") as f:
                        f.write(response.content)
                    # os.replace preserves the TEMP file's mode (0666 & umask),
                    # not the previous inode's — pin it explicitly like the
                    # organizations publish does (#203): a restrictive
                    # deploy-time umask must not leave the published file
                    # unreadable to the download endpoint's process.
                    os.chmod(tmp_path, 0o644)
                    os.replace(tmp_path, file_path)
                except BaseException:
                    tmp_path.unlink(missing_ok=True)
                    raise
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
            skip_existing: Skip if a COMPLETE JSON already exists (see below)

        Returns:
            True if successful, False otherwise
        """
        # Check if already downloaded. A JSON marked `_comments_incomplete`
        # (comment pagination failed mid-fetch) is exactly the issue that needs
        # re-fetching: without this, `--skip-existing` — the default — makes the
        # marker permanent, so the issue is never re-fetched and never heals.
        json_path = self.issues_dir / f"{issue_key}.json"
        if skip_existing and json_path.exists() and not _needs_refetch(json_path):
            self.stats["skipped"] += 1
            return True

        # Fetch issue
        issue_data = self.fetch_issue(issue_key)
        if not issue_data:
            self.stats["failed"] += 1
            return False

        # Fetch and embed remote links for Parquet transform. If fetch fails,
        # leave the key ABSENT so transform_remote_links preserves existing rows.
        try:
            issue_data["_remote_links"] = self.fetch_remote_links(issue_key)
        except JiraFetchError as e:
            logger.warning(
                f"Skipping _remote_links overlay for {issue_key}: {e}. Existing parquet rows will be preserved."
            )

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
        logger.info("Starting Jira backfill")
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
            # Count existing — the same skip decision a real --skip-existing
            # run would make (marker-aware), not a bare .exists(), so this
            # doesn't under-report against what a real run would re-fetch
            # (Devin Review on #1283).
            existing = _count_already_downloaded(self.issues_dir, issue_keys)
            logger.info(f"Already downloaded: {existing}")
            logger.info(f"Would download: {total_issues - existing}")
            return {"total": total_issues, "existing": existing}

        # Process issues in parallel
        processed = 0
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            # Submit all tasks
            futures = {executor.submit(self.process_issue, key, skip_existing): key for key in issue_keys}

            # Process as completed
            for future in as_completed(futures):
                issue_key = futures[future]
                processed += 1

                try:
                    future.result()
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
        logger.info(f"Time: {elapsed:.1f}s ({total_issues / elapsed:.1f} issues/s)")
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
                # Same marker-aware skip decision as the real run — see
                # _count_already_downloaded (Devin Review on #1283).
                existing = _count_already_downloaded(backfill.issues_dir, issue_keys)
                logger.info(f"Already downloaded: {existing}")
                logger.info(f"Would download: {len(issue_keys) - existing}")
                sys.exit(0)

            # Process each issue
            from concurrent.futures import ThreadPoolExecutor, as_completed

            start_time = time.time()
            processed = 0

            with ThreadPoolExecutor(max_workers=args.parallel) as executor:
                futures = {executor.submit(backfill.process_issue, key, args.skip_existing): key for key in issue_keys}

                for future in as_completed(futures):
                    issue_key = futures[future]
                    processed += 1

                    try:
                        future.result()
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
