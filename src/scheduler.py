"""
Schedule evaluation for automatic data sync.

Parses sync_schedule strings from table configuration and determines
whether a table is due for synchronization based on its last sync time.

Schedule formats:
    "every 15m"   - every 15 minutes
    "every 1h"    - every hour
    "daily 05:00" - once per day at 05:00 UTC
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Pattern: "every 15m", "every 2h"
INTERVAL_PATTERN = re.compile(r"^every (\d+)([mh])$")

# Pattern: "daily 05:00", "daily 17:30"
DAILY_PATTERN = re.compile(r"^daily (\d{2}):(\d{2})$")


def parse_interval_minutes(schedule: str) -> Optional[int]:
    """Parse an interval schedule into minutes.

    Args:
        schedule: Schedule string like "every 15m" or "every 1h"

    Returns:
        Interval in minutes, or None if not an interval schedule.
    """
    match = INTERVAL_PATTERN.match(schedule)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    if unit == "h":
        return value * 60
    return value


def is_table_due(
    schedule: str,
    last_sync_iso: Optional[str],
    now: Optional[datetime] = None,
) -> bool:
    """Determine whether a table is due for sync based on its schedule.

    Args:
        schedule: Schedule string from table config (e.g., "every 1h", "daily 05:00")
        last_sync_iso: ISO timestamp of last sync, or None if never synced
        now: Current time (UTC). Defaults to datetime.now(timezone.utc).

    Returns:
        True if the table should be synced now.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Never synced -> always due
    if not last_sync_iso:
        logger.info("Table never synced, marking as due")
        return True

    # Parse last_sync timestamp
    last_sync = _parse_timestamp(last_sync_iso)
    if last_sync is None:
        logger.warning(f"Cannot parse last_sync timestamp: {last_sync_iso}, marking as due")
        return True

    # Ensure timezone-aware comparison
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)

    # Check interval schedule: "every Xm" / "every Xh"
    interval_minutes = parse_interval_minutes(schedule)
    if interval_minutes is not None:
        elapsed_minutes = (now - last_sync).total_seconds() / 60
        due = elapsed_minutes >= interval_minutes
        if due:
            logger.debug(
                f"Interval schedule: {elapsed_minutes:.0f}m elapsed >= {interval_minutes}m interval"
            )
        return due

    # Check daily schedule: "daily HH:MM"
    match = DAILY_PATTERN.match(schedule)
    if match:
        target_hour = int(match.group(1))
        target_minute = int(match.group(2))
        return _is_daily_due(last_sync, now, target_hour, target_minute)

    logger.warning(f"Unknown schedule format: {schedule}")
    return False


def _is_daily_due(
    last_sync: datetime,
    now: datetime,
    target_hour: int,
    target_minute: int,
) -> bool:
    """Check if a daily schedule is due.

    A daily schedule at HH:MM is due when:
    1. Current time is at or past HH:MM today, AND
    2. Last sync was before HH:MM today

    This means: once HH:MM passes, the first scheduler tick will trigger it,
    and subsequent ticks on the same day will skip it.
    """
    # Today's target time
    today_target = now.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0
    )

    # Not yet time today
    if now < today_target:
        return False

    # Time has passed, check if we already synced after today's target
    if last_sync >= today_target:
        return False

    logger.debug(
        f"Daily schedule: target {target_hour:02d}:{target_minute:02d} UTC, "
        f"last sync {last_sync.isoformat()}, now {now.isoformat()} -> due"
    )
    return True


def _parse_timestamp(iso_string: str) -> Optional[datetime]:
    """Parse an ISO timestamp string, handling various formats.

    Args:
        iso_string: ISO 8601 timestamp string

    Returns:
        datetime object or None if parsing fails
    """
    try:
        # Python 3.11+ fromisoformat handles most formats
        return datetime.fromisoformat(iso_string)
    except (ValueError, TypeError):
        pass

    # Fallback: try common formats
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(iso_string, fmt)
        except ValueError:
            continue

    return None
