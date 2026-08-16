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

from app.logging_config import setup_logging
from connectors.jira.file_lock import issue_json_lock
from connectors.jira.incremental_transform import transform_issues
from connectors.jira.scripts.backfill_sla import (
    configured_field_ids,
    load_config,
)

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
            logger.warning(
                f"Failed to fetch fields+status for {issue_key}: {response.status_code} {response.text[:200]}"
            )
            return None

    except httpx.RequestError as e:
        logger.error(f"Request error fetching fields+status for {issue_key}: {e}")
        return None


def find_open_issues(parquet_dir: Path) -> tuple[list[str], int]:
    """Open-ticket keys from the issues parquets, plus the unreadable-file count.

    Skipping an unreadable partition is right for making progress — the other
    months' tickets still get polled — but it silently removes every open
    ticket of that month from the working set, so their raw JSON stops being
    refreshed and their SLA data goes stale at the source. The count exists so
    `run()` can fold that into `failed` instead of the poll getting faster and
    greener as the hole grows.
    """
    issues_dir = parquet_dir / "issues"
    if not issues_dir.exists():
        logger.error(f"Issues Parquet directory not found: {issues_dir}")
        return [], 0

    # Recursive: matches both flat (<table>/<YYYY-MM>.parquet) and hive
    # (<table>/month=<YYYY-MM>/data.parquet) Jira parquet layouts.
    parquet_files = sorted(issues_dir.rglob("*.parquet"))
    if not parquet_files:
        logger.error(f"No Parquet files found in {issues_dir}")
        return [], 0

    logger.info(f"Reading {len(parquet_files)} Parquet files from {issues_dir}")

    columns = ["issue_key", "status_category"]
    dfs = []
    unreadable = 0
    for pf in parquet_files:
        try:
            df = pd.read_parquet(pf, columns=columns)
            dfs.append(df)
        except Exception as e:
            unreadable += 1
            logger.error(f"Failed to read {pf} — its open tickets are not being polled: {e}")

    if not dfs:
        return [], unreadable

    all_issues = pd.concat(dfs, ignore_index=True)
    logger.info(f"Total issues in Parquet: {len(all_issues)}")

    open_issues = all_issues[all_issues["status_category"] != "Done"]
    issue_keys = open_issues["issue_key"].tolist()
    logger.info(f"Open issues: {len(issue_keys)}")
    return issue_keys, unreadable


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

    # Fetch fresh field + status data from API
    api_data = fetch_sla_and_status(base_url, auth, issue_key)
    if api_data is None:
        logger.warning(f"Failed to fetch fields+status for {issue_key}")
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

        # The parquet write is deliberately NOT done here. `run()` batches it:
        # it groups the whole poll by month and applies each month in one
        # read-modify-write per table (`transform_issues`). The JSON above is
        # durable and is exactly what that pass reads, so nothing is lost by
        # returning now — and this issue's lock is released before any month lock
        # is taken, which is what keeps `file_lock.py`'s documented
        # issue(outer) -> month(inner) nesting intact.

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

    config = load_config()  # loads .env so JIRA_REFRESH_FIELDS is available below
    raw_dir = config["data_dir"]
    parquet_dir = Path(os.environ.get("JIRA_PARQUET_DIR", "/data/src_data/parquet/jira"))
    base_url = config["base_url"]
    auth = (config["email"], config["api_token"])

    field_ids = configured_field_ids()
    if not field_ids:
        logger.warning(
            "JIRA_REFRESH_FIELDS is not configured — no fields to refresh; skipping poll. "
            "Set JIRA_REFRESH_FIELDS to e.g. 'customfield_10328:first_response' to enable."
        )
        return {
            "open_issues": 0,
            "updated": 0,
            "healed": 0,
            "skipped": 0,
            "failed": 0,
            "elapsed_sec": 0.0,
            "dry_run": dry_run,
        }

    open_issues, unreadable_partitions = find_open_issues(parquet_dir)

    stats = {
        "open_issues": len(open_issues),
        "updated": 0,
        "healed": 0,
        "skipped": 0,
        # Unreadable partitions removed their open tickets from the working set,
        # where the refreshed-minus-written delta below can never see them —
        # they are failures from the start, and dry runs report them too.
        # (`find_open_issues` already named each file at ERROR.)
        "failed": unreadable_partitions,
        "elapsed_sec": 0.0,
        "dry_run": dry_run,
    }

    if not open_issues:
        logger.info("No open issues found")
        return stats

    if dry_run:
        logger.info(f"Dry run: would poll {len(open_issues)} open issues")
        return stats

    start_time = time.time()

    # Phase 1 — refresh each ticket's raw JSON. No parquet is touched here, so the
    # only lock held is that issue's own `issue_json_lock`.
    refreshed: list[str] = []
    for i, issue_key in enumerate(sorted(open_issues), 1):
        logger.info(f"[{i}/{len(open_issues)}] Polling {issue_key}...")
        result = update_issue_sla(issue_key, raw_dir, base_url, auth)
        stats[result] += 1
        if result in ("updated", "healed"):
            refreshed.append(issue_key)
        time.sleep(0.5)  # gentle on the Jira API

    # Phase 2 — land them, one read-modify-write per table per month.
    #
    # This used to run inside phase 1: every ticket called `transform_single_issue`,
    # which rewrites its month's SIX partitions in full. A month holding N polled
    # tickets was therefore rewritten N times per cycle, re-emitting bytes the
    # previous ticket had just written — the dominant cost of a poll, and enough
    # churn that `sync_state`'s hashes could never settle against it.
    #
    # `transform_issues` derives each issue's month from its own `created_at` and
    # does the grouping itself. It is deliberately not this module's job: `month`
    # is a hive DIRECTORY key, never a column in the parquet, so there is nothing
    # here to read it back from.
    written = transform_issues(refreshed, raw_dir=raw_dir)
    # Set-based, not length-based: `find_open_issues` concats every parquet file
    # without dedup, the same key legitimately sits in two partitions, and
    # `transform_issues` dedups — a length delta would read that as a permanent
    # phantom failure. `written` is a subset of `refreshed`, so this is exactly
    # the tickets whose parquet write did not land; the per-month isolation
    # already logged why, and this fold is the only failure signal left.
    unapplied = len(set(refreshed) - set(written))
    if unapplied:
        stats["failed"] += unapplied
        logger.error(
            f"{unapplied} refreshed ticket(s) did not land in parquet — see 'Could not apply month' errors above"
        )
    if refreshed:
        logger.info(f"Applied {len(written)} of {len(refreshed)} refreshed ticket(s)")

    elapsed = time.time() - start_time

    # One coalesced rebuild for the whole run — not one per issue, and not none
    # at all. `transform_single_issue` no longer refreshes `_meta` itself (that
    # moved to `app.worker.kinds._run_jira_refresh`), and this module, unlike the
    # webhook path in `connectors/jira/service.py`, has never enqueued anything.
    # Without this the catalog's row/size numbers — and the `CREATE OR REPLACE
    # VIEW` that `update_meta` also performs — would only be refreshed whenever a
    # webhook happened to fire, which on an instance where this poller is the main
    # writer could be never. The parquet the poll just wrote is queryable either
    # way: the extract views glob it per query.
    #
    # Skipped when nothing was written — a rebuild with nothing to publish is pure
    # cost, and this runs every 15 minutes.
    if stats["updated"] or stats["healed"]:
        try:
            from app.job_correlation import stamp_request_id
            from src.repositories import jobs_repo

            result = jobs_repo().enqueue("jira-refresh", stamp_request_id({}), idempotency_key="jira-refresh")
            # Same invariant the webhook path states in `connectors/jira/service.py`:
            # the dedup matches `status IN ('queued', 'running')`, so collapsing onto
            # an already-RUNNING refresh is not enough — that job may have read the
            # parquet before this run's writes landed. Enqueue a coalescing follow-up
            # under a distinct key to guarantee a rebuild strictly after them. Nothing
            # else recovers it: the next poll only enqueues if it writes again.
            if result.get("status") == "running":
                jobs_repo().enqueue("jira-refresh", stamp_request_id({}), idempotency_key="jira-refresh-followup")
        except Exception as enqueue_err:
            # Non-fatal by design: this module also runs as a standalone script
            # where the job queue need not be reachable, and the poll's own work
            # — fresh JSON, fresh parquet — is already done and durable.
            logger.warning(f"Could not enqueue jira-refresh after the SLA poll: {enqueue_err}")

    logger.info("=" * 60)
    logger.info("Field refresh polling completed!")
    logger.info(f"Open issues polled: {len(open_issues)}")
    logger.info(f"Updated (fields only): {stats['updated']}")
    logger.info(f"Healed (status corrected): {stats['healed']}")
    logger.info(f"Skipped: {stats['skipped']}")
    logger.info(f"Failed: {stats['failed']}")
    logger.info(f"Time: {elapsed:.1f}s")
    logger.info("=" * 60)

    stats["elapsed_sec"] = elapsed
    return stats


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
