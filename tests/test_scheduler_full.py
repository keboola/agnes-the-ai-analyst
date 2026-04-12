"""Tests for schedule parsing and due-check logic in src/scheduler.py."""

from datetime import datetime, timezone

import pytest

from src.scheduler import is_table_due, parse_interval_minutes


# ---------------------------------------------------------------------------
# parse_interval_minutes
# ---------------------------------------------------------------------------

class TestParseIntervalMinutes:
    def test_every_15m(self):
        assert parse_interval_minutes("every 15m") == 15

    def test_every_1h(self):
        assert parse_interval_minutes("every 1h") == 60

    def test_every_2h(self):
        assert parse_interval_minutes("every 2h") == 120

    def test_every_30m(self):
        assert parse_interval_minutes("every 30m") == 30

    def test_daily_returns_none(self):
        assert parse_interval_minutes("daily 05:00") is None

    def test_invalid_string_returns_none(self):
        assert parse_interval_minutes("gibberish") is None

    def test_empty_string_returns_none(self):
        assert parse_interval_minutes("") is None

    def test_every_0m(self):
        # Edge case: zero minutes is still a valid parse
        assert parse_interval_minutes("every 0m") == 0


# ---------------------------------------------------------------------------
# is_table_due  — interval schedules
# ---------------------------------------------------------------------------

def _utc(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


class TestIsTableDueNeverSynced:
    def test_never_synced_is_always_due(self):
        assert is_table_due("every 1h", last_sync_iso=None) is True

    def test_empty_string_last_sync_is_due(self):
        assert is_table_due("every 1h", last_sync_iso="") is True


class TestIsTableDueInterval:
    NOW = _utc(2026, 4, 12, 10, 0, 0)

    def test_interval_not_elapsed(self):
        # Synced 10 minutes ago, interval is 30m
        last = _utc(2026, 4, 12, 9, 50, 0).isoformat()
        assert is_table_due("every 30m", last, now=self.NOW) is False

    def test_interval_elapsed(self):
        # Synced 31 minutes ago, interval is 30m
        last = _utc(2026, 4, 12, 9, 29, 0).isoformat()
        assert is_table_due("every 30m", last, now=self.NOW) is True

    def test_exact_boundary(self):
        # Synced exactly 30 minutes ago — boundary is inclusive (>=)
        last = _utc(2026, 4, 12, 9, 30, 0).isoformat()
        assert is_table_due("every 30m", last, now=self.NOW) is True

    def test_interval_1h_not_elapsed(self):
        last = _utc(2026, 4, 12, 9, 30, 0).isoformat()
        assert is_table_due("every 1h", last, now=self.NOW) is False

    def test_interval_1h_elapsed(self):
        last = _utc(2026, 4, 12, 8, 59, 0).isoformat()
        assert is_table_due("every 1h", last, now=self.NOW) is True


class TestIsTableDueDaily:
    def test_before_target_time_not_due(self):
        # now is 04:00 UTC, target is 05:00 — not yet reached
        now = _utc(2026, 4, 12, 4, 0, 0)
        last = _utc(2026, 4, 11, 5, 0, 0).isoformat()  # yesterday
        assert is_table_due("daily 05:00", last, now=now) is False

    def test_after_target_time_due(self):
        # now is 06:00 UTC, target is 05:00 — past target
        now = _utc(2026, 4, 12, 6, 0, 0)
        last = _utc(2026, 4, 11, 5, 0, 0).isoformat()  # last sync was yesterday
        assert is_table_due("daily 05:00", last, now=now) is True

    def test_already_synced_today(self):
        # Now is 10:00 UTC, synced at 05:30 today — not due again
        now = _utc(2026, 4, 12, 10, 0, 0)
        last = _utc(2026, 4, 12, 5, 30, 0).isoformat()
        assert is_table_due("daily 05:00", last, now=now) is False

    def test_daily_multiple_times_second_time_due(self):
        # Schedule: daily 07:00,13:00,18:00
        # Now is 14:00, already synced at 07:30 — second target (13:00) is due
        now = _utc(2026, 4, 12, 14, 0, 0)
        last = _utc(2026, 4, 12, 7, 30, 0).isoformat()
        assert is_table_due("daily 07:00,13:00,18:00", last, now=now) is True

    def test_daily_multiple_times_not_due_after_all(self):
        # Now is 19:00, synced at 18:30 — all targets passed
        now = _utc(2026, 4, 12, 19, 0, 0)
        last = _utc(2026, 4, 12, 18, 30, 0).isoformat()
        assert is_table_due("daily 07:00,13:00,18:00", last, now=now) is False


class TestIsTableDueEdgeCases:
    def test_unknown_format_returns_false(self):
        now = _utc(2026, 4, 12, 10, 0, 0)
        assert is_table_due("weekly monday", "2026-04-11T09:00:00", now=now) is False

    def test_invalid_timestamp_treated_as_due(self):
        assert is_table_due("every 1h", "not-a-timestamp") is True

    def test_naive_last_sync_timestamp(self):
        # ISO timestamp without timezone info should still work
        now = _utc(2026, 4, 12, 10, 0, 0)
        last = "2026-04-12T08:00:00"  # no tz info
        assert is_table_due("every 1h", last, now=now) is True

    def test_none_now_uses_current_time(self):
        # Simply smoke-test that it doesn't crash with now=None
        result = is_table_due("every 1h", last_sync_iso=None, now=None)
        assert result is True  # never synced
