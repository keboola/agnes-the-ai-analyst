#!/usr/bin/env python3
"""
Jira field backfill - fetch the configured refresh fields into existing issues.

One-time migration script that fetches the operator-configured custom fields from
the Jira API with the primary token (JIRA_EMAIL / JIRA_API_TOKEN) and embeds them
into existing issue JSON files. The token's account needs whatever read permission
each field requires (e.g. a JSM Agent licence for SLA fields). The field ids come
from JIRA_REFRESH_FIELDS (no defaults); discover them with
`verify_sla_access --list-fields`.

NOTE: A classic API token uses the site domain URL
(https://your-org.atlassian.net/rest/api/3/...). A scoped token must use the
gateway URL (https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/...);
set JIRA_CLOUD_ID to switch to it.

Usage:
    # On server:
    python -m connectors.jira.scripts.backfill_sla

    # With parallel workers:
    python -m connectors.jira.scripts.backfill_sla --parallel 8

    # Dry run (count files needing update):
    python -m connectors.jira.scripts.backfill_sla --dry-run

    # Force re-fetch even if SLA data already present:
    python -m connectors.jira.scripts.backfill_sla --force

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from dotenv import load_dotenv

from connectors.jira.service import refresh_fields

from app.logging_config import setup_logging

setup_logging(__name__)
logger = logging.getLogger(__name__)


def configured_field_ids() -> list[str]:
    """Field ids from JIRA_REFRESH_FIELDS (no defaults), resolved at call time."""
    return [fid for fid, _ in refresh_fields()]


def load_config() -> dict:
    """Load configuration from environment variables."""
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

    required = ["JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN"]
    missing = [var for var in required if not os.environ.get(var)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    cloud_id = os.environ.get("JIRA_CLOUD_ID", "")
    if cloud_id:
        # Scoped API tokens must use the api.atlassian.com gateway.
        base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
    else:
        # Classic API token: the site domain URL serves the same REST API.
        base_url = f"https://{os.environ['JIRA_DOMAIN']}/rest/api/3"

    return {
        "email": os.environ["JIRA_EMAIL"],
        "api_token": os.environ["JIRA_API_TOKEN"],
        "base_url": base_url,
        "data_dir": Path(os.environ.get("JIRA_DATA_DIR", "/data/src_data/raw/jira")),
        "refresh_fields": configured_field_ids(),
    }


def needs_field_update(data: dict) -> bool:
    """True if any configured field is missing/null/error in the issue JSON."""
    fields = data.get("fields", {})
    for field_id in configured_field_ids():
        value = fields.get(field_id)
        if value is None or (isinstance(value, dict) and "errorMessage" in value):
            return True
    return False


def fetch_fields(base_url: str, auth: tuple[str, str], issue_key: str) -> dict | None:
    """
    Fetch SLA fields for a single issue from Jira API.

    Returns dict with SLA field values, or None on failure.
    """
    url = f"{base_url}/issue/{issue_key}"
    params = {"fields": ",".join(configured_field_ids())}

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
            return fetch_fields(base_url, auth, issue_key)
        else:
            logger.warning(f"Failed to fetch SLA for {issue_key}: {response.status_code} {response.text[:200]}")
            return None

    except httpx.RequestError as e:
        logger.error(f"Request error fetching SLA for {issue_key}: {e}")
        return None


def process_file(json_path: Path, base_url: str, auth: tuple[str, str], force: bool) -> str:
    """
    Process a single issue JSON file - fetch and embed SLA data.

    Returns: "updated", "skipped", or "failed"
    """
    try:
        with open(json_path) as f:
            data = json.load(f)

        # Check if update needed
        if not force and not needs_field_update(data):
            return "skipped"

        issue_key = data.get("key")
        if not issue_key:
            return "failed"

        # Fetch SLA fields from API
        sla_data = fetch_fields(base_url, auth, issue_key)
        if sla_data is None:
            return "failed"

        # Update fields in raw JSON
        if "fields" not in data:
            data["fields"] = {}

        for sla_field in configured_field_ids():
            if sla_field in sla_data:
                data["fields"][sla_field] = sla_data[sla_field]

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

        return "updated"

    except Exception as e:
        logger.error(f"Error processing {json_path.name}: {e}")
        return "failed"


def main():
    parser = argparse.ArgumentParser(
        description="Backfill SLA fields into existing Jira issue JSONs",
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
        help="Only count files needing update, don't fetch or modify",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch SLA data even if already present",
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

    field_ids = configured_field_ids()
    if not field_ids:
        logger.warning(
            "JIRA_REFRESH_FIELDS is not configured — nothing to backfill. "
            "Set JIRA_REFRESH_FIELDS to e.g. 'customfield_10328:first_response' to enable."
        )
        sys.exit(0)

    base_url = config["base_url"]
    auth = (config["email"], config["api_token"])

    # Enumerate JSON files
    json_files = sorted(issues_dir.glob("*.json"))
    total = len(json_files)
    logger.info(f"Found {total} issue JSON files in {issues_dir}")
    logger.info(f"API base URL: {base_url}")

    if args.dry_run:
        needs_update = 0
        has_error = 0
        has_valid = 0
        has_none = 0

        for jf in json_files:
            try:
                with open(jf) as f:
                    data = json.load(f)
                if needs_field_update(data):
                    needs_update += 1
                    fields = data.get("fields", {})
                    sla_ids = configured_field_ids()
                    frt = fields.get(sla_ids[0]) if sla_ids else None
                    if isinstance(frt, dict) and "errorMessage" in frt:
                        has_error += 1
                    elif frt is None:
                        has_none += 1
                else:
                    has_valid += 1
            except Exception:
                needs_update += 1

        logger.info(f"Already have valid SLA data: {has_valid}")
        logger.info(f"Have permission error: {has_error}")
        logger.info(f"Have NULL/missing: {has_none}")
        logger.info(f"Total needing update: {needs_update}")
        return

    # Process files in parallel
    stats = {"updated": 0, "skipped": 0, "failed": 0}
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {executor.submit(process_file, jf, base_url, auth, args.force): jf for jf in json_files}

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
                    f"updated: {stats['updated']}, "
                    f"skipped: {stats['skipped']}, "
                    f"failed: {stats['failed']}"
                )

    elapsed = time.time() - start_time

    logger.info("=" * 60)
    logger.info("SLA backfill completed!")
    logger.info(f"Total files: {total}")
    logger.info(f"Updated: {stats['updated']}")
    logger.info(f"Skipped (already had valid SLA): {stats['skipped']}")
    logger.info(f"Failed: {stats['failed']}")
    logger.info(f"Time: {elapsed:.1f}s")
    logger.info("=" * 60)

    if stats["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
