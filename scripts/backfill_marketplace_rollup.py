"""One-shot backfill of `usage_marketplace_item_daily` + `_window` from existing usage_events.

After v45->v46 migrates the schema, the new rollup tables start empty. The
UsageProcessor rebuilds them on its first tick (7-day incremental for daily
fact + 7d window) and within an hour fills `last_30d`, so doing nothing is
also a valid path — but the marketplace pages then show zero invocations
for up to 30 days until events older than the 7-day incremental window
flow back into the rollup.

This script forces a full rebuild from `usage_events` immediately via the
backend-aware `usage_repo().rebuild_rollups()` (#728 — the free function
this script used to call directly was DuckDB-only and left rollups
permanently empty on Postgres). Safe to re-run — idempotent (DELETE+INSERT).

Usage::

    python scripts/backfill_marketplace_rollup.py
"""

from __future__ import annotations

import logging
import sys

from src.repositories import usage_repo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main() -> int:
    repo = usage_repo()

    # since_day=None -> full rebuild: the repo computes the effective cutoff
    # as the earliest day present in usage_events, so the daily-fact rebuild
    # covers all historical data, not just the default 7-day incremental
    # window. force_30d=True refreshes the 30d window snapshot immediately
    # too, instead of waiting for the hourly throttle.
    log.info("backfill_marketplace_rollup: full rebuild, force_30d=True")
    repo.rebuild_rollups(force_30d=True)

    if hasattr(repo, "conn"):  # DuckDB
        daily_rows = repo.conn.execute("SELECT COUNT(*) FROM usage_marketplace_item_daily").fetchone()[0]
        window_rows = repo.conn.execute(
            "SELECT period_label, COUNT(*) FROM usage_marketplace_item_window GROUP BY 1"
        ).fetchall()
    else:  # Postgres
        import sqlalchemy as sa

        with repo._engine.connect() as conn:
            daily_rows = conn.execute(sa.text("SELECT COUNT(*) FROM usage_marketplace_item_daily")).scalar()
            window_rows = conn.execute(
                sa.text("SELECT period_label, COUNT(*) FROM usage_marketplace_item_window GROUP BY 1")
            ).fetchall()

    log.info(
        "backfill complete: daily=%d window=%s",
        daily_rows,
        dict(window_rows),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
