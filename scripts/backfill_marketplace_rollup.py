"""One-shot backfill of `usage_marketplace_item_daily` + `_window` from existing usage_events.

After v45→v46 migrates the schema, the new rollup tables start empty. The
UsageProcessor rebuilds them on its first tick (7-day incremental for daily
fact + 7d window) and within an hour fills `last_30d`, so doing nothing is
also a valid path — but the marketplace pages then show zero invocations
for up to 30 days until events older than the 7-day incremental window
flow back into the rollup.

This script forces a full rebuild from `usage_events` immediately. Safe
to re-run — uses the same `rebuild_rollups()` function the scheduler
invokes, which is idempotent (DELETE+INSERT).

Usage::

    python scripts/backfill_marketplace_rollup.py
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from services.session_processors.usage_lib import rebuild_rollups
from src.db import get_system_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main() -> int:
    conn = get_system_db()

    # Find the oldest usage_event so the daily-fact rebuild covers all
    # historical data, not just the default 7-day incremental window.
    row = conn.execute("SELECT MIN(CAST(occurred_at AS DATE)) FROM usage_events").fetchone()
    since_day = row[0] if row and row[0] else datetime.now(timezone.utc).date()

    log.info("backfill_marketplace_rollup: since_day=%s, force_30d=True", since_day)
    rebuild_rollups(conn, since_day=since_day, force_30d=True)

    daily_rows = conn.execute("SELECT COUNT(*) FROM usage_marketplace_item_daily").fetchone()[0]
    window_rows = conn.execute("SELECT period_label, COUNT(*) FROM usage_marketplace_item_window GROUP BY 1").fetchall()
    log.info(
        "backfill complete: daily=%d window=%s",
        daily_rows,
        dict(window_rows),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
