"""
Schedule evaluation for automatic data sync.

Parses sync_schedule strings from table configuration and determines
whether a table is due for synchronization based on its last sync time.

Schedule formats:
    "every 15m"            - every 15 minutes
    "every 1h"             - every hour
    "daily 05:00"          - once per day at 05:00 UTC
    "daily 07:00,13:00,18:00" - multiple times per day (UTC)
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Pattern: "every 15m", "every 2h"
INTERVAL_PATTERN = re.compile(r"^every (\d+)([mh])$")

# Pattern: "daily 05:00", "daily 17:30", "daily 07:00,13:00,18:00"
DAILY_PATTERN = re.compile(r"^daily ([\d:,]+)$")


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

    # Check daily schedule: "daily HH:MM" or "daily HH:MM,HH:MM,..."
    match = DAILY_PATTERN.match(schedule)
    if match:
        times_str = match.group(1)
        target_times = _parse_daily_times(times_str)
        if not target_times:
            logger.warning(f"Invalid daily schedule times: {schedule}")
            return False
        return _is_daily_due(last_sync, now, target_times)

    logger.warning(f"Unknown schedule format: {schedule}")
    return False


def _parse_daily_times(times_str: str) -> list[tuple[int, int]]:
    """Parse comma-separated HH:MM times into list of (hour, minute) tuples."""
    time_pattern = re.compile(r"^(\d{2}):(\d{2})$")
    result = []
    for part in times_str.split(","):
        m = time_pattern.match(part.strip())
        if not m:
            return []
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return []
        result.append((hour, minute))
    return result


def _is_daily_due(
    last_sync: datetime,
    now: datetime,
    target_times: list[tuple[int, int]],
) -> bool:
    """Check if a daily schedule is due.

    Supports multiple target times per day. A target time is due when:
    1. Current time is at or past HH:MM today, AND
    2. Last sync was before HH:MM today

    Returns True if ANY of the target times is due.
    """
    for target_hour, target_minute in target_times:
        today_target = now.replace(
            hour=target_hour, minute=target_minute, second=0, microsecond=0
        )

        if now >= today_target and last_sync < today_target:
            logger.debug(
                f"Daily schedule: target {target_hour:02d}:{target_minute:02d} UTC, "
                f"last sync {last_sync.isoformat()}, now {now.isoformat()} -> due"
            )
            return True

    return False


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


def is_valid_schedule(schedule: Optional[str]) -> bool:
    """Return True iff ``schedule`` parses as a documented schedule string.

    Accepted forms (mirroring the rest of this module):
      - ``"every Nm"`` / ``"every Nh"`` with N a positive integer
      - ``"daily HH:MM"`` (24-h, UTC) optionally comma-separated:
        ``"daily 07:00,13:00"``

    Anything else — including ``None``, empty string, or a parseable-looking
    but out-of-range value (``"daily 25:00"``) — returns False. Pydantic
    validators on the admin API call this to reject malformed input with
    422 instead of accepting it and silently no-op'ing later.
    """
    if not schedule or not isinstance(schedule, str):
        return False
    interval = parse_interval_minutes(schedule)
    if interval is not None:
        return interval > 0
    match = DAILY_PATTERN.match(schedule)
    if not match:
        return False
    return bool(_parse_daily_times(match.group(1)))


def filter_due_tables(
    table_configs: list[dict],
    sync_state_repo,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Drop table configs whose ``sync_schedule`` says they are not due.

    Behaviour:
      - ``sync_schedule`` is None / empty / not a valid string → table passes
        through (no schedule = "sync on every tick", existing behaviour).
      - Valid schedule + last_sync within the cadence → drop.
      - Valid schedule + last_sync past cadence (or never) → keep.
      - Invalid schedule string → log a warning and let the table through
        (do NOT silently skip — operator surprise is worse than a redundant
        sync).

    ``sync_state_repo`` is duck-typed: only ``get_last_sync(table_id)`` is
    called, returning a ``datetime`` (tz-aware preferred, naive treated as
    UTC) or ``None``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    out: list[dict] = []
    for tc in table_configs:
        schedule = tc.get("sync_schedule")
        if not schedule:
            out.append(tc)
            continue
        if not is_valid_schedule(schedule):
            logger.warning(
                "Table %s has malformed sync_schedule %r — syncing anyway "
                "(fix the schedule string to suppress this message)",
                tc.get("id") or tc.get("name"),
                schedule,
            )
            out.append(tc)
            continue
        last_sync = sync_state_repo.get_last_sync(tc.get("id") or tc.get("name"))
        last_sync_iso: Optional[str]
        if last_sync is None:
            last_sync_iso = None
        else:
            if last_sync.tzinfo is None:
                last_sync = last_sync.replace(tzinfo=timezone.utc)
            last_sync_iso = last_sync.isoformat()
        if is_table_due(schedule, last_sync_iso, now=now):
            out.append(tc)
        else:
            logger.info(
                "Table %s skipped: schedule=%r, last_sync=%s, not due yet",
                tc.get("id") or tc.get("name"),
                schedule,
                last_sync_iso,
            )
    return out
