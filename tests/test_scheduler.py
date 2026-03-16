"""Tests for src.scheduler - schedule parsing and sync-due evaluation."""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from src.scheduler import (
    _is_daily_due,
    _parse_daily_times,
    _parse_timestamp,
    is_table_due,
    parse_interval_minutes,
)

# Fixed reference time: 2026-03-15 12:00:00 UTC
NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# parse_interval_minutes
# ---------------------------------------------------------------------------


class TestParseIntervalMinutes:
    """Tests for parse_interval_minutes()."""

    def test_minutes_basic(self) -> None:
        assert parse_interval_minutes("every 15m") == 15

    def test_minutes_single_digit(self) -> None:
        assert parse_interval_minutes("every 5m") == 5

    def test_minutes_large(self) -> None:
        assert parse_interval_minutes("every 120m") == 120

    def test_hours_basic(self) -> None:
        assert parse_interval_minutes("every 2h") == 120

    def test_hours_single(self) -> None:
        assert parse_interval_minutes("every 1h") == 60

    def test_hours_large(self) -> None:
        assert parse_interval_minutes("every 24h") == 1440

    def test_daily_returns_none(self) -> None:
        assert parse_interval_minutes("daily 05:00") is None

    def test_invalid_format_returns_none(self) -> None:
        assert parse_interval_minutes("not a schedule") is None

    def test_empty_string_returns_none(self) -> None:
        assert parse_interval_minutes("") is None

    def test_missing_unit_returns_none(self) -> None:
        assert parse_interval_minutes("every 15") is None

    def test_wrong_unit_returns_none(self) -> None:
        assert parse_interval_minutes("every 15s") is None

    def test_no_space_returns_none(self) -> None:
        assert parse_interval_minutes("every15m") is None

    def test_extra_whitespace_returns_none(self) -> None:
        # Strict parsing: extra whitespace is rejected
        assert parse_interval_minutes("every  15m") is None

    def test_negative_not_matched(self) -> None:
        # Regex uses \d+ so negative sign won't match
        assert parse_interval_minutes("every -5m") is None

    def test_zero_minutes(self) -> None:
        # "every 0m" matches the pattern, returns 0
        assert parse_interval_minutes("every 0m") == 0


# ---------------------------------------------------------------------------
# is_table_due - interval schedules
# ---------------------------------------------------------------------------


class TestIsTableDueInterval:
    """Tests for is_table_due() with interval-based schedules."""

    def test_never_synced_is_due(self) -> None:
        assert is_table_due("every 15m", last_sync_iso=None, now=NOW) is True

    def test_empty_last_sync_is_due(self) -> None:
        assert is_table_due("every 15m", last_sync_iso="", now=NOW) is True

    def test_synced_10min_ago_every_15m_not_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=10)).isoformat()
        assert is_table_due("every 15m", last_sync_iso=last_sync, now=NOW) is False

    def test_synced_20min_ago_every_15m_is_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=20)).isoformat()
        assert is_table_due("every 15m", last_sync_iso=last_sync, now=NOW) is True

    def test_synced_exactly_15min_ago_every_15m_is_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=15)).isoformat()
        assert is_table_due("every 15m", last_sync_iso=last_sync, now=NOW) is True

    def test_synced_30min_ago_every_1h_not_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=30)).isoformat()
        assert is_table_due("every 1h", last_sync_iso=last_sync, now=NOW) is False

    def test_synced_90min_ago_every_1h_is_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=90)).isoformat()
        assert is_table_due("every 1h", last_sync_iso=last_sync, now=NOW) is True

    def test_synced_exactly_1h_ago_every_1h_is_due(self) -> None:
        last_sync = (NOW - timedelta(hours=1)).isoformat()
        assert is_table_due("every 1h", last_sync_iso=last_sync, now=NOW) is True

    def test_synced_59min_ago_every_1h_not_due(self) -> None:
        last_sync = (NOW - timedelta(minutes=59)).isoformat()
        assert is_table_due("every 1h", last_sync_iso=last_sync, now=NOW) is False

    def test_synced_3h_ago_every_2h_is_due(self) -> None:
        last_sync = (NOW - timedelta(hours=3)).isoformat()
        assert is_table_due("every 2h", last_sync_iso=last_sync, now=NOW) is True


# ---------------------------------------------------------------------------
# is_table_due - daily schedules
# ---------------------------------------------------------------------------


class TestIsTableDueDaily:
    """Tests for is_table_due() with daily schedules."""

    def test_before_target_time_not_due(self) -> None:
        now = datetime(2026, 3, 15, 4, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 6, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 05:00", last_sync_iso=last_sync, now=now) is False

    def test_past_target_not_synced_today_is_due(self) -> None:
        now = datetime(2026, 3, 15, 5, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 4, 0, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 05:00", last_sync_iso=last_sync, now=now) is True

    def test_past_target_already_synced_after_target_not_due(self) -> None:
        now = datetime(2026, 3, 15, 5, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 5, 15, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 05:00", last_sync_iso=last_sync, now=now) is False

    def test_evening_schedule_past_target_last_sync_yesterday_is_due(self) -> None:
        now = datetime(2026, 3, 15, 18, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 17, 30, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 17:00", last_sync_iso=last_sync, now=now) is True

    def test_daily_never_synced_is_due(self) -> None:
        now = datetime(2026, 3, 15, 6, 0, 0, tzinfo=timezone.utc)
        assert is_table_due("daily 05:00", last_sync_iso=None, now=now) is True

    def test_daily_never_synced_before_target_still_due(self) -> None:
        # Never synced always returns True regardless of target time
        now = datetime(2026, 3, 15, 3, 0, 0, tzinfo=timezone.utc)
        assert is_table_due("daily 05:00", last_sync_iso=None, now=now) is True

    def test_daily_exactly_at_target_time_is_due(self) -> None:
        now = datetime(2026, 3, 15, 5, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 5, 0, 0, tzinfo=timezone.utc).isoformat()
        # now == today_target, so now < today_target is False
        # last_sync (yesterday) < today_target => due
        assert is_table_due("daily 05:00", last_sync_iso=last_sync, now=now) is True

    def test_daily_synced_at_exactly_target_not_due_again(self) -> None:
        now = datetime(2026, 3, 15, 5, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 5, 0, 0, tzinfo=timezone.utc).isoformat()
        # last_sync == today_target => last_sync >= today_target => not due
        assert is_table_due("daily 05:00", last_sync_iso=last_sync, now=now) is False

    def test_midnight_schedule(self) -> None:
        now = datetime(2026, 3, 15, 0, 30, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 0, 15, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 00:00", last_sync_iso=last_sync, now=now) is True

    def test_end_of_day_schedule(self) -> None:
        now = datetime(2026, 3, 15, 23, 59, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 23, 50, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 23:30", last_sync_iso=last_sync, now=now) is True


# ---------------------------------------------------------------------------
# is_table_due - edge cases
# ---------------------------------------------------------------------------


class TestIsTableDueEdgeCases:
    """Edge case tests for is_table_due()."""

    def test_unparseable_last_sync_returns_true(self) -> None:
        # Fail-safe: if we can't parse last_sync, assume sync is needed
        assert is_table_due("every 15m", last_sync_iso="garbage", now=NOW) is True

    def test_unknown_schedule_format_returns_false(self) -> None:
        last_sync = (NOW - timedelta(hours=2)).isoformat()
        assert is_table_due("weekly", last_sync_iso=last_sync, now=NOW) is False

    def test_unknown_schedule_never_synced_returns_true(self) -> None:
        # Never synced takes priority over unknown schedule
        assert is_table_due("weekly", last_sync_iso=None, now=NOW) is True

    def test_now_defaults_to_current_time(self) -> None:
        # When now is not provided, it defaults to current UTC time
        # A table that was never synced should be due regardless
        assert is_table_due("every 15m", last_sync_iso=None) is True

    def test_naive_last_sync_treated_as_utc(self) -> None:
        # Naive timestamp (no timezone) should be treated as UTC
        naive_ts = "2026-03-15T11:50:00"
        # 10 minutes ago from NOW (12:00), with 15m interval -> not due
        assert is_table_due("every 15m", last_sync_iso=naive_ts, now=NOW) is False

    def test_last_sync_in_future_not_due(self) -> None:
        # Edge case: last_sync in the future (clock skew, etc.)
        future = (NOW + timedelta(hours=1)).isoformat()
        assert is_table_due("every 15m", last_sync_iso=future, now=NOW) is False


# ---------------------------------------------------------------------------
# _is_daily_due (internal function, direct tests)
# ---------------------------------------------------------------------------


class TestIsDailyDue:
    """Direct tests for _is_daily_due() internal function."""

    def test_before_target_not_due(self) -> None:
        now = datetime(2026, 3, 15, 4, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 5, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(5, 0)]) is False

    def test_after_target_last_sync_before_target_is_due(self) -> None:
        now = datetime(2026, 3, 15, 6, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 4, 0, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(5, 0)]) is True

    def test_after_target_last_sync_after_target_not_due(self) -> None:
        now = datetime(2026, 3, 15, 6, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 5, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(5, 0)]) is False

    def test_target_with_minutes(self) -> None:
        now = datetime(2026, 3, 15, 17, 45, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(17, 30)]) is True

    def test_target_with_minutes_not_yet(self) -> None:
        now = datetime(2026, 3, 15, 17, 15, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(17, 30)]) is False


class TestMultipleDailyTimes:
    """Tests for multiple daily schedule times."""

    def test_multi_time_first_due(self) -> None:
        now = datetime(2026, 3, 15, 8, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 14, 19, 0, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(7, 0), (13, 0), (18, 0)]) is True

    def test_multi_time_second_due(self) -> None:
        now = datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 7, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(7, 0), (13, 0), (18, 0)]) is True

    def test_multi_time_third_due(self) -> None:
        now = datetime(2026, 3, 15, 19, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 13, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(7, 0), (13, 0), (18, 0)]) is True

    def test_multi_time_between_slots_not_due(self) -> None:
        now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 7, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(7, 0), (13, 0), (18, 0)]) is False

    def test_multi_time_all_done_not_due(self) -> None:
        now = datetime(2026, 3, 15, 20, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 18, 30, 0, tzinfo=timezone.utc)
        assert _is_daily_due(last_sync, now, [(7, 0), (13, 0), (18, 0)]) is False

    def test_is_table_due_multi_time_format(self) -> None:
        now = datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 7, 30, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 07:00,13:00,18:00", last_sync_iso=last_sync, now=now) is True

    def test_is_table_due_multi_time_not_due(self) -> None:
        now = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        last_sync = datetime(2026, 3, 15, 7, 30, 0, tzinfo=timezone.utc).isoformat()
        assert is_table_due("daily 07:00,13:00,18:00", last_sync_iso=last_sync, now=now) is False


class TestParseDailyTimes:
    """Tests for _parse_daily_times()."""

    def test_single_time(self) -> None:
        assert _parse_daily_times("05:00") == [(5, 0)]

    def test_multiple_times(self) -> None:
        assert _parse_daily_times("07:00,13:00,18:00") == [(7, 0), (13, 0), (18, 0)]

    def test_invalid_format(self) -> None:
        assert _parse_daily_times("7:00") == []

    def test_invalid_hour(self) -> None:
        assert _parse_daily_times("25:00") == []

    def test_invalid_minute(self) -> None:
        assert _parse_daily_times("12:60") == []


# ---------------------------------------------------------------------------
# _parse_timestamp
# ---------------------------------------------------------------------------


class TestParseTimestamp:
    """Tests for _parse_timestamp() internal function."""

    def test_iso_with_timezone(self) -> None:
        result = _parse_timestamp("2026-03-15T12:00:00+00:00")
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 15
        assert result.hour == 12

    def test_iso_with_z_suffix(self) -> None:
        # Python 3.11+ fromisoformat handles Z
        result = _parse_timestamp("2026-03-15T12:00:00Z")
        assert result is not None
        assert result.hour == 12

    def test_iso_without_timezone(self) -> None:
        result = _parse_timestamp("2026-03-15T12:00:00")
        assert result is not None
        assert result.hour == 12
        assert result.tzinfo is None

    def test_iso_with_microseconds(self) -> None:
        result = _parse_timestamp("2026-03-15T12:00:00.123456")
        assert result is not None
        assert result.microsecond == 123456

    def test_space_separated(self) -> None:
        result = _parse_timestamp("2026-03-15 12:00:00")
        assert result is not None
        assert result.hour == 12

    def test_invalid_string_returns_none(self) -> None:
        assert _parse_timestamp("not-a-date") is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_timestamp("") is None

    def test_partial_date_returns_none(self) -> None:
        # "2026-03-15" alone - fromisoformat handles date-only in 3.11+
        result = _parse_timestamp("2026-03-15")
        # Should parse as a date (with hour=0, minute=0)
        assert result is not None
        assert result.hour == 0

    def test_iso_with_positive_offset(self) -> None:
        result = _parse_timestamp("2026-03-15T12:00:00+05:30")
        assert result is not None
        assert result.hour == 12
        assert result.utcoffset() is not None

    def test_iso_with_negative_offset(self) -> None:
        result = _parse_timestamp("2026-03-15T12:00:00-07:00")
        assert result is not None
        assert result.utcoffset() is not None

    def test_numeric_garbage_returns_none(self) -> None:
        assert _parse_timestamp("12345") is None

    def test_none_like_string_returns_none(self) -> None:
        assert _parse_timestamp("None") is None
