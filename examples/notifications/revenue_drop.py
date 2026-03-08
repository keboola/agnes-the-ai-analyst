#!/usr/bin/env python3
"""
Example notification: Revenue drop alert (text only).

Checks if today's revenue dropped significantly vs 7-day average.
Outputs JSON to stdout for notify-runner.
"""

import json
import sys
from pathlib import Path

import duckdb

# Configuration
DB_PATH = Path.home() / "user" / "duckdb" / "analytics.duckdb"
DROP_THRESHOLD_PERCENT = 20


def check_revenue_drop() -> dict:
    """Query revenue data and check for significant drop."""
    if not DB_PATH.exists():
        return {"notify": False}

    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)

        # Example query - adjust table/column names to your schema
        result = conn.execute("""
            WITH daily AS (
                SELECT
                    DATE_TRUNC('day', created_at) AS day,
                    SUM(amount) AS revenue
                FROM payments
                WHERE created_at >= CURRENT_DATE - INTERVAL '8 days'
                GROUP BY 1
            ),
            stats AS (
                SELECT
                    (SELECT revenue FROM daily WHERE day = CURRENT_DATE) AS today_rev,
                    AVG(CASE WHEN day < CURRENT_DATE AND day >= CURRENT_DATE - INTERVAL '7 days'
                        THEN revenue END) AS avg_7d
                FROM daily
            )
            SELECT today_rev, avg_7d,
                   ROUND((1 - today_rev / NULLIF(avg_7d, 0)) * 100, 1) AS drop_pct
            FROM stats
        """).fetchone()

        conn.close()

        if result is None or result[0] is None or result[1] is None:
            return {"notify": False}

        today_rev, avg_7d, drop_pct = result

        if drop_pct >= DROP_THRESHOLD_PERCENT:
            return {
                "notify": True,
                "title": f"Revenue dropped {drop_pct}%",
                "message": (
                    f"Today: ${today_rev:,.0f}\n"
                    f"7d avg: ${avg_7d:,.0f}\n"
                    f"Drop: {drop_pct}%"
                ),
                "cooldown": "6h",
                "data": {
                    "today_revenue": float(today_rev),
                    "avg_7d_revenue": float(avg_7d),
                    "drop_percent": float(drop_pct),
                },
            }

        return {"notify": False}

    except Exception as e:
        print(f"revenue_drop error: {e}", file=sys.stderr)
        return {"notify": False}


if __name__ == "__main__":
    result = check_revenue_drop()
    print(json.dumps(result))
