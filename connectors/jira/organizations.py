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
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
import pyarrow.parquet as pq

from connectors.jira.extract_init import get_default_output_dir, update_meta
from connectors.jira.service import (
    JiraFetchError,
    get_jira_service,
    organization_detail_fields,
    reload_config_from_env,
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

# Refuse to publish a sweep that would drop more than this fraction of the rows the
# table already had, unless the caller explicitly forces it.
#
# Two ways rows disappear, and neither is reported in `failed`:
#   * an enumerated id 404s on the CSM read. A genuinely deleted organization is
#     simply absent from enumeration, so it never reaches that path — a 404 here means
#     enumerated-but-unreadable, i.e. a race, a wrong cloud id, or a token that can
#     list through the Service Desk API but not read through CSM (Devin Review #1274);
#   * enumeration returns a short list without raising, so organizations vanish by
#     omission.
# An all-or-nothing failure is already safe (no rows resolve, the write is skipped),
# but a partial one silently deletes the invisible half. This gives deletion the same
# "abort rather than publish something suspicious" treatment enumeration failure gets.
#
# It does NOT clear itself: skipping the write leaves the table its old size, so a
# real bulk cleanup trips the guard on every subsequent run too. That is deliberate —
# a human should look — and `--force` is the escape hatch.
MAX_REMOVED_FRACTION = 0.5

# `skipped_reason` values that mean the run did not publish what it should have.
#
# Single source of truth for both surfaces: the CLI turns these into a non-zero exit,
# and the worker handler raises so the job is retried and the refusal is visible in job
# history. They previously disagreed — the CLI exited 1 on a total outage while the
# scheduled path logged and finalized `done`, so a broken dimension looked healthy every
# day and only an ERROR log line said otherwise (Devin Review on #1274).
#
# `jira_not_configured` is deliberately NOT here: an instance without Jira ingest is
# expected to skip this job, not to fail it every night.
FAILURE_REASONS = frozenset({"all_fetches_failed", "mass_removal_guard", "existing_unreadable"})


def _read_existing(table_dir: Path) -> dict[str, dict] | None:
    """Existing rows keyed by ``org_id``.

    Returns ``{}`` only when there genuinely is no table yet, and ``None`` when one
    exists but its previous state could not be established.

    The distinction is load-bearing, because this one value feeds both safety nets in
    ``refresh_organizations``: rows are carried forward for organizations whose fetch
    failed (``if org_id in existing``), and the mass-removal guard is skipped entirely
    when ``existing`` is falsy. Collapsing an unreadable table into ``{}`` therefore
    disabled both at once — a transient IO or pyarrow error alongside any per-organization
    API failure would republish only whatever resolved this run and silently delete the
    rest, which is precisely the failure the guard exists to prevent (Devin Review on
    #1274). The caller refuses to publish on ``None`` instead.
    """
    parquet_path = table_dir / "data.parquet"
    if not parquet_path.exists():
        return {}
    try:
        df = pd.read_parquet(parquet_path)
    except Exception as e:
        logger.error("Could not read existing %s: %s", parquet_path, e)
        return None
    if "org_id" not in df.columns:
        # A table without the key column is not a baseline we can reason about either.
        logger.error("Existing %s has no org_id column — refusing to treat it as empty", parquet_path)
        return None
    return {str(row["org_id"]): row for row in df.to_dict("records") if row.get("org_id") is not None}


def refresh_organizations(
    extract_dir: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Rebuild the organizations table from the Jira API.

    Enumerates every organization, fetches each one's details, and writes a single
    unpartitioned parquet. Enumeration failures abort before any write — a partial
    list would look like organizations had been deleted. Per-organization fetch
    failures are non-fatal but *do not* blank the row: the previous values are carried
    forward, so a transient 429 or 5xx costs freshness, never data. Only a 404
    (organization genuinely gone) drops a row.

    A sweep that would drop more than ``MAX_REMOVED_FRACTION`` of the existing rows is
    refused rather than published; ``force=True`` overrides that.

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

    extract_path = extract_dir or get_default_output_dir()
    table_dir = extract_path / "data" / TABLE_NAME
    existing = _read_existing(table_dir)
    if existing is None:
        # A table exists but its contents could not be established. Publishing now would
        # run with both safety nets down — no rows to carry forward, and the removal
        # guard skipped for want of a baseline — so the next API hiccup would delete
        # whatever it could not fetch. Refuse instead; the read is retried next run.
        logger.error(
            "Cannot establish the current %s contents — refusing to publish. Re-run once the parquet is readable.",
            TABLE_NAME,
        )
        stats["skipped_reason"] = "existing_unreadable"
        stats["elapsed_sec"] = round(time.time() - start, 1)
        return stats

    records: list[dict] = []
    # One client for the whole sweep. Per-request clients cost a fresh TLS handshake
    # (and an SSL context rebuild) per organization — measured at ~100ms each, i.e.
    # a third of the wall clock on a few-hundred-organization site, entirely
    # separately from the rate-limit pacing below.
    with httpx.Client(timeout=30) as client:
        for i, org_id in enumerate(org_ids, 1):
            # Paced at the top of the body so every exit path below inherits it —
            # a `continue` added later cannot accidentally drop the rate limit —
            # and so the last organization is not followed by a pointless sleep.
            if i > 1:
                time.sleep(REQUEST_DELAY_SEC)
            if i % 50 == 0:
                logger.info("Fetched %d/%d organizations", i, len(org_ids))

            try:
                raw_org = service.fetch_organization(org_id, client=client)
            except JiraFetchError as e:
                # Keep the previous row rather than dropping or blanking the organization.
                logger.warning("Organization %s: %s", org_id, e)
                stats["failed"] += 1
                if org_id in existing:
                    records.append(existing[org_id])
                    stats["preserved"] += 1
                continue

            if raw_org is None:
                # 404: the organization no longer exists, so it should leave the table.
                logger.info("Organization %s no longer exists — dropping from %s", org_id, TABLE_NAME)
                stats["removed"] += 1
                continue

            records.append(transform_organization(raw_org))
            stats["written"] += 1

    # Nothing fresh resolved. Two shapes: a first run where everything failed (no
    # records at all) and an outage on an existing table, where every row in `records`
    # is a preserved copy of what is already on disk. Both are total failures, and the
    # second one used to fall through and republish a byte-identical parquet, refresh
    # `_meta` and enqueue a rebuild — work that publishes nothing, on the one code path
    # where the API is known to be down (Devin Review on #1274).
    if not stats["written"] and not stats["removed"]:
        logger.error(
            "No organization rows resolved (%d failed, %d preserved) — leaving %s untouched",
            stats["failed"],
            stats["preserved"],
            TABLE_NAME,
        )
        stats["skipped_reason"] = "all_fetches_failed"
        stats["elapsed_sec"] = round(time.time() - start, 1)
        return stats

    # Mass-deletion guard. See MAX_REMOVED_FRACTION: rows vanish both by 404 and by
    # simple omission from a short enumeration, and neither shows up in `failed`.
    #
    # Counted as an actual removal set, not as a net size change. Comparing totals
    # (`len(existing) - len(records)`) lets organizations added in the same sweep cancel
    # out ones that disappeared, so on any growing site the guard could not fire at all:
    # 10 existing, 10 new, 6 of the old ones unreadable gives a net of -4 while 60% of
    # the table is being deleted (Devin Review on #1274).
    if existing and not force:
        resolved_ids = {str(r.get("org_id")) for r in records if r.get("org_id") is not None}
        dropped = sum(1 for org_id in existing if org_id not in resolved_ids)
        if dropped / len(existing) > MAX_REMOVED_FRACTION:
            logger.error(
                "Refusing to publish %s: %d of %d existing organizations would be removed "
                "(%.0f%%, over the %.0f%% limit) — the sweep resolved %d rows from %d "
                "enumerated ids, %d of which 404'd. Check JIRA_CLOUD_ID and that the "
                "account has Customer Service Management access. Re-run with --force if "
                "the removals are real.",
                TABLE_NAME,
                dropped,
                len(existing),
                100 * dropped / len(existing),
                100 * MAX_REMOVED_FRACTION,
                len(records),
                len(org_ids),
                stats["removed"],
            )
            stats["skipped_reason"] = "mass_removal_guard"
            stats["elapsed_sec"] = round(time.time() - start, 1)
            return stats

    table_dir.mkdir(parents=True, exist_ok=True)
    table = apply_schema(pd.DataFrame(records), organizations_schema())
    dest = table_dir / "data.parquet"
    # Write-then-replace so a reader never observes a truncated parquet: the extract
    # views glob this directory on every query, including mid-write.
    tmp_dest = table_dir / "data.parquet.tmp"
    try:
        pq.write_table(table, tmp_dest, **PARQUET_WRITE_OPTIONS)
        os.replace(tmp_dest, dest)
    finally:
        # A failed write would otherwise leave `data.parquet.tmp` behind, and the
        # extract view globs `*.parquet` — a stray `.tmp` is not matched, but the
        # sibling writers (connectors/keboola/incremental.py) clean up for the same
        # reason and an accumulating temp file is nobody's friend.
        tmp_dest.unlink(missing_ok=True)

    # Refresh the catalog row + rebuild the view so the new column set is visible.
    #
    # Under `rebuild_mutex()` for the reason `app.worker.kinds._run_jira_refresh`
    # documents: `update_meta` opens extract.duckdb for writing while a rebuild
    # elsewhere may have it ATTACHed, and DuckDB is single-writer. Only this call is
    # inside the mutex — holding it across the fetch loop above would block every
    # rebuild for minutes of network I/O.
    try:
        from src.orchestrator import rebuild_mutex

        with rebuild_mutex():
            update_meta(extract_path, TABLE_NAME)
    except Exception as e:
        # Non-fatal: the parquet is durable and the extract view globs it per query.
        logger.warning("Could not update _meta for %s: %s", TABLE_NAME, e)

    # Publish. `update_meta` above creates the view inside extract.duckdb, but the
    # view in the master analytics database is only built by a rebuild — and
    # `_attach_and_create_views` SKIPS any `_meta` row whose inner object did not
    # exist when it ran ("Skipping master view for %s.%s — no inner object",
    # src/orchestrator.py). On the first refresh the row exists with 0 rows and no
    # view, so without this enqueue the table stays invisible to queries until some
    # unrelated rebuild happens to fire. Same coalescing pattern (and the same
    # running-job follow-up) as the SLA poll and the webhook path.
    try:
        from app.job_correlation import stamp_request_id
        from src.repositories import jobs_repo

        result = jobs_repo().enqueue("jira-refresh", stamp_request_id({}), idempotency_key="jira-refresh")
        if result.get("status") == "running":
            jobs_repo().enqueue("jira-refresh", stamp_request_id({}), idempotency_key="jira-refresh-followup")
    except Exception as enqueue_err:
        # Non-fatal by design: this module also runs as a standalone script where the
        # job queue need not be reachable, and the parquet is already durable.
        logger.warning("Could not enqueue jira-refresh after the organization refresh: %s", enqueue_err)

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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Publish even if the sweep would drop most of the existing rows (see MAX_REMOVED_FRACTION)",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    from app.logging_config import setup_logging

    setup_logging(__name__)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load the .env the way every other Jira CLI in this connector does. Loading it is
    # not enough on its own: `_JiraConfig` snapshots os.environ in its class body, which
    # ran when this module imported `connectors.jira.service` at the top of the file —
    # long before main(). `load_dotenv()` only writes into os.environ, so without the
    # reload below the service still holds empty credentials and the documented manual
    # run exits 0 reporting "Jira is not configured" (Devin Review on #1274).
    from connectors.jira.scripts.backfill_sla import load_config

    try:
        load_config()
    except ValueError as e:
        # `load_config` validates JIRA_DOMAIN/EMAIL/API_TOKEN and raises when any is
        # missing. It is called here only for its load_dotenv() side effect, so that
        # validation would turn the documented run — `--dry-run` included — into an
        # unhandled traceback on an instance without Jira configured, instead of the
        # clean "not configured" skip refresh_organizations already reports (Devin
        # Review on #1274). The dotenv load has already happened by the time it raises.
        logger.debug("Jira env validation failed while loading .env: %s", e)
    reload_config_from_env()

    stats = refresh_organizations(dry_run=args.dry_run, force=args.force)
    # Non-zero on the outcomes FAILURE_REASONS names, so a cron/CI caller notices. A
    # *partial* failure is success: those rows resolved and the rest kept their previous
    # values, which is the contract this module exists to honour.
    if stats.get("skipped_reason") in FAILURE_REASONS:
        sys.exit(1)


if __name__ == "__main__":
    main()
