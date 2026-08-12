"""Refresh the current-state `organizations` table from the JSM organization API.

Tickets carry organization *ids* (`issues.organization_ids`); this module resolves
those ids to a name plus whichever organization detail fields the operator configured
in ``JIRA_ORG_DETAIL_FIELDS``. Together they let a ticket be joined to whatever the
detail field points at (a CRM account id, a region, an account owner) without
matching on organization names, which drift whenever an organization is renamed.

Low-frequency by design: organization membership and details change rarely, so this
runs on a daily schedule rather than per webhook. See
``app.worker.kinds._run_jira_org_refresh``.

Usage:
    python -m connectors.jira.organizations
    python -m connectors.jira.organizations --dry-run
"""

import argparse
import logging
import os
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from connectors.jira.extract_init import update_meta
from connectors.jira.service import (
    JiraFetchError,
    get_jira_service,
    organization_detail_fields,
)
from connectors.jira.transform import (
    PARQUET_WRITE_OPTIONS,
    apply_schema,
    organizations_schema,
    transform_organization,
)

logger = logging.getLogger(__name__)

TABLE_NAME = "organizations"

# Pause between per-organization requests. The CSM API has no documented bulk read
# that returns detail *ids*, so this is one request per organization; the delay keeps
# a few-hundred-organization site well clear of Jira's rate limiter on a job that
# only runs daily.
REQUEST_DELAY_SEC = 0.2


def default_extract_dir() -> Path:
    """The Jira extract directory (the one holding ``extract.duckdb``)."""
    return Path(os.environ.get("DATA_DIR", "/data")) / "extracts" / "jira"


def _read_existing(table_dir: Path) -> dict[str, dict]:
    """Existing rows keyed by ``org_id``, or ``{}`` when there is no parquet yet.

    Used to preserve rows for organizations whose fetch failed this run — see
    ``refresh_organizations``.
    """
    parquet_path = table_dir / "data.parquet"
    if not parquet_path.exists():
        return {}
    try:
        df = pd.read_parquet(parquet_path)
    except Exception as e:
        logger.warning("Could not read existing %s: %s", parquet_path, e)
        return {}
    if "org_id" not in df.columns:
        return {}
    return {str(row["org_id"]): dict(row) for _, row in df.iterrows() if row.get("org_id") is not None}


def refresh_organizations(
    extract_dir: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Rebuild the organizations table from the Jira API.

    Enumerates every organization, fetches each one's details, and writes a single
    unpartitioned parquet. Enumeration failures abort before any write — a partial
    list would look like organizations had been deleted. Per-organization fetch
    failures are non-fatal but *do not* blank the row: the previous values are carried
    forward, so a transient 429 or 5xx costs freshness, never data. Only a 404
    (organization genuinely gone) drops a row.

    Returns a stats dict: ``organizations``, ``written``, ``preserved``, ``removed``,
    ``failed``, ``elapsed_sec``, ``dry_run``, and ``skipped_reason`` when it did not run.
    """
    stats: dict = {
        "organizations": 0,
        "written": 0,
        "preserved": 0,
        "removed": 0,
        "failed": 0,
        "elapsed_sec": 0.0,
        "dry_run": dry_run,
    }

    service = get_jira_service()
    if not service.is_configured():
        logger.info("Jira is not configured — skipping organization refresh")
        stats["skipped_reason"] = "jira_not_configured"
        return stats

    start = time.time()

    # Abort on enumeration failure: writing a partial list would delete rows for every
    # organization the failed page would have contained.
    org_ids = service.fetch_organization_ids()
    stats["organizations"] = len(org_ids)

    if not org_ids:
        logger.info("Jira returned no organizations — nothing to refresh")
        stats["elapsed_sec"] = round(time.time() - start, 1)
        return stats

    if dry_run:
        configured = [column for _, column in organization_detail_fields()]
        logger.info(
            "Dry run: would refresh %d organizations into columns %s",
            len(org_ids),
            ["org_id", "name", *configured],
        )
        stats["elapsed_sec"] = round(time.time() - start, 1)
        return stats

    extract_path = extract_dir or default_extract_dir()
    table_dir = extract_path / "data" / TABLE_NAME
    existing = _read_existing(table_dir)

    records: list[dict] = []
    for i, org_id in enumerate(org_ids, 1):
        try:
            raw_org = service.fetch_organization(org_id)
        except JiraFetchError as e:
            # Keep the previous row rather than dropping or blanking the organization.
            logger.warning("Organization %s: %s", org_id, e)
            stats["failed"] += 1
            if org_id in existing:
                records.append(existing[org_id])
                stats["preserved"] += 1
            time.sleep(REQUEST_DELAY_SEC)
            continue

        if raw_org is None:
            # 404: the organization no longer exists, so it should leave the table.
            logger.info("Organization %s no longer exists — dropping from %s", org_id, TABLE_NAME)
            stats["removed"] += 1
            time.sleep(REQUEST_DELAY_SEC)
            continue

        records.append(transform_organization(raw_org))
        stats["written"] += 1

        if i % 50 == 0:
            logger.info("Fetched %d/%d organizations", i, len(org_ids))
        time.sleep(REQUEST_DELAY_SEC)

    if not records:
        logger.warning(
            "No organization rows resolved (%d failed) — leaving %s untouched",
            stats["failed"],
            TABLE_NAME,
        )
        stats["elapsed_sec"] = round(time.time() - start, 1)
        return stats

    table_dir.mkdir(parents=True, exist_ok=True)
    table = apply_schema(pd.DataFrame(records), organizations_schema())
    dest = table_dir / "data.parquet"
    # Write-then-replace so a reader never observes a truncated parquet: the extract
    # views glob this directory on every query, including mid-write.
    tmp_dest = table_dir / "data.parquet.tmp"
    pq.write_table(table, tmp_dest, **PARQUET_WRITE_OPTIONS)
    os.replace(tmp_dest, dest)

    # Refresh the catalog row + rebuild the view so the new column set is visible.
    #
    # Under `rebuild_mutex()` for the reason `app.worker.kinds._run_jira_refresh`
    # documents: `update_meta` opens extract.duckdb for writing while a rebuild
    # elsewhere may have it ATTACHed, and DuckDB is single-writer. Only this call is
    # inside the mutex — holding it across the fetch loop above would block every
    # rebuild for minutes of network I/O. The import is local and optional because
    # this module also runs as a standalone script where the orchestrator (and its
    # dependencies) need not be importable.
    try:
        try:
            from src.orchestrator import rebuild_mutex
        except Exception:  # pragma: no cover - standalone/script use
            rebuild_mutex = None
        if rebuild_mutex is not None:
            with rebuild_mutex():
                update_meta(extract_path, TABLE_NAME)
        else:
            update_meta(extract_path, TABLE_NAME)
    except Exception as e:
        # Non-fatal: the parquet is durable and the view globs it per query.
        logger.warning("Could not update _meta for %s: %s", TABLE_NAME, e)

    stats["elapsed_sec"] = round(time.time() - start, 1)
    logger.info(
        "Organization refresh complete: %d written, %d preserved, %d removed, %d failed in %.1fs",
        stats["written"],
        stats["preserved"],
        stats["removed"],
        stats["failed"],
        stats["elapsed_sec"],
    )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the Jira organizations table")
    parser.add_argument("--dry-run", action="store_true", help="Enumerate only; do not fetch or write")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    from app.logging_config import setup_logging

    setup_logging(__name__)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    refresh_organizations(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
