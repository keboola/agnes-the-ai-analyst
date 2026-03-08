#!/usr/bin/env python3
"""
Example notification: Daily metric report with chart image.

Generates a summary chart using matplotlib and sends it as a Telegram photo.
Outputs JSON to stdout for notify-runner.
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import duckdb

DB_PATH = Path.home() / "user" / "duckdb" / "analytics.duckdb"


def generate_chart(data: list[tuple]) -> str | None:
    """Generate a bar chart and return the file path."""
    try:
        import matplotlib

        matplotlib.use("Agg")  # Non-interactive backend
        import matplotlib.pyplot as plt

        dates = [row[0].strftime("%m/%d") for row in data]
        values = [float(row[1]) for row in data]

        fig, ax = plt.subplots(figsize=(8, 4))
        bars = ax.bar(dates, values, color="#0073D1", width=0.6)

        # Highlight today
        if len(bars) > 0:
            bars[-1].set_color("#EA580C")

        ax.set_title("Daily Revenue - Last 7 Days", fontsize=14, fontweight="bold")
        ax.set_ylabel("Revenue ($)")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()

        # Save to temp file
        chart_path = os.path.join(
            tempfile.gettempdir(),
            f"notify_{os.environ.get('USER', 'user')}_metric_{datetime.now():%Y%m%d}.png",
        )
        plt.savefig(chart_path, dpi=150, bbox_inches="tight")
        plt.close()

        return chart_path

    except ImportError:
        print("matplotlib not installed, skipping chart", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Chart generation error: {e}", file=sys.stderr)
        return None


def build_report() -> dict:
    """Build daily metric report."""
    if not DB_PATH.exists():
        return {"notify": False}

    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)

        # TODO: Adapt this query to your schema.
        # DuckDB views use relative paths, so scripts must run from ~/
        # (notify-runner sets cwd to home directory automatically).
        #
        # Example for a table with date + numeric columns:
        #   SELECT DATE_TRUNC('day', created_at)::DATE AS day,
        #          SUM(amount) AS revenue
        #   FROM my_table
        #   WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
        #   GROUP BY 1 ORDER BY 1
        rows = conn.execute("""
            SELECT
                DATE_TRUNC('day', created_at)::DATE AS day,
                COUNT(*) AS cnt
            FROM kbc_project
            WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY 1
            ORDER BY 1
        """).fetchall()

        conn.close()

        if not rows:
            return {"notify": False}

        today_val = float(rows[-1][1]) if rows else 0
        total_7d = sum(float(r[1]) for r in rows)

        chart_path = generate_chart(rows)

        result = {
            "notify": True,
            "title": "Daily Metric Report",
            "message": (
                f"Today: {today_val:,.0f}\n"
                f"7d total: {total_7d:,.0f}\n"
                f"7d avg: {total_7d / len(rows):,.0f}"
            ),
            "cooldown": "1d",
        }

        if chart_path:
            result["image_path"] = chart_path

        return result

    except Exception as e:
        print(f"metric_report error: {e}", file=sys.stderr)
        return {"notify": False}


if __name__ == "__main__":
    result = build_report()
    print(json.dumps(result))
