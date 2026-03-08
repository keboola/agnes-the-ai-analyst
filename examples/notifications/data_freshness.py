#!/usr/bin/env python3
"""
Example notification: Data freshness alert.

Checks if the local data is stale (older than threshold) and notifies.
Outputs JSON to stdout for notify-runner.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Configuration
DATA_DIR = Path.home() / "server" / "parquet"
STALE_THRESHOLD_HOURS = 24


def check_freshness() -> dict:
    """Check data freshness by examining parquet file modification times."""
    if not DATA_DIR.exists():
        return {
            "notify": True,
            "title": "Data directory missing",
            "message": f"Data directory not found: {DATA_DIR}\nRun: bash scripts/sync_data.sh",
            "cooldown": "6h",
        }

    # Find newest parquet file
    parquet_files = list(DATA_DIR.rglob("*.parquet"))
    if not parquet_files:
        return {
            "notify": True,
            "title": "No data files found",
            "message": "No parquet files in data directory.\nRun: bash scripts/sync_data.sh",
            "cooldown": "6h",
        }

    newest_mtime = max(f.stat().st_mtime for f in parquet_files)
    newest_dt = datetime.fromtimestamp(newest_mtime, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    age_hours = (now - newest_dt).total_seconds() / 3600

    if age_hours > STALE_THRESHOLD_HOURS:
        return {
            "notify": True,
            "title": f"Data is {age_hours:.0f}h old",
            "message": (
                f"Latest file: {newest_dt:%Y-%m-%d %H:%M} UTC\n"
                f"Age: {age_hours:.1f} hours (threshold: {STALE_THRESHOLD_HOURS}h)\n"
                f"Run: bash scripts/sync_data.sh"
            ),
            "cooldown": "6h",
        }

    return {"notify": False}


if __name__ == "__main__":
    result = check_freshness()
    print(json.dumps(result))
