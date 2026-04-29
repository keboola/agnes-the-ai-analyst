"""Tests for the schedule-validity helper and the per-table due-filter."""

from datetime import datetime, timezone

import pytest

from src.scheduler import filter_due_tables, is_valid_schedule


# ---------------- is_valid_schedule -----------------------------------------

@pytest.mark.parametrize("schedule", [
    "every 15m",
    "every 1h",
    "every 6h",
    "daily 05:00",
    "daily 07:00,13:00,18:00",
])
def test_is_valid_schedule_accepts_documented_formats(schedule):
    assert is_valid_schedule(schedule) is True


@pytest.mark.parametrize("schedule", [
    "",
    "every",
    "every 0m",          # zero is not a positive interval
    "every 15s",         # seconds not supported
    "daily",
    "daily 25:00",       # invalid hour
    "daily 12:60",       # invalid minute
    "daily 12:00,",      # trailing comma
    "hourly",            # unknown keyword
    "every -5m",         # negative
])
def test_is_valid_schedule_rejects_malformed_strings(schedule):
    assert is_valid_schedule(schedule) is False


def test_is_valid_schedule_treats_none_as_invalid():
    # None is "no schedule" — callers handle that case before validating.
    # The validator is for non-null strings only.
    assert is_valid_schedule(None) is False  # type: ignore[arg-type]


# ---------------- filter_due_tables -----------------------------------------

class _FakeSyncStateRepo:
    """Stub SyncStateRepository — returns last_sync per table_id."""

    def __init__(self, last_syncs: dict[str, datetime | None]):
        self._data = last_syncs

    def get_last_sync(self, table_id: str):
        return self._data.get(table_id)


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_filter_due_tables_passes_through_unscheduled_tables():
    """Tables with sync_schedule=None are always due (opt-in feature)."""
    configs = [
        {"id": "t1", "name": "t1", "sync_schedule": None},
        {"id": "t2", "name": "t2", "sync_schedule": ""},
    ]
    repo = _FakeSyncStateRepo({})
    out = filter_due_tables(configs, repo, now=_utc(2026, 5, 1, 10, 0))
    assert [c["id"] for c in out] == ["t1", "t2"]


def test_filter_due_tables_drops_table_within_interval():
    """A table on 'every 1h' synced 30m ago is NOT due."""
    configs = [{"id": "fast", "name": "fast", "sync_schedule": "every 1h"}]
    repo = _FakeSyncStateRepo({"fast": _utc(2026, 5, 1, 9, 30)})
    out = filter_due_tables(configs, repo, now=_utc(2026, 5, 1, 10, 0))
    assert out == []


def test_filter_due_tables_keeps_table_past_interval():
    """A table on 'every 1h' synced 90m ago IS due."""
    configs = [{"id": "fast", "name": "fast", "sync_schedule": "every 1h"}]
    repo = _FakeSyncStateRepo({"fast": _utc(2026, 5, 1, 8, 30)})
    out = filter_due_tables(configs, repo, now=_utc(2026, 5, 1, 10, 0))
    assert [c["id"] for c in out] == ["fast"]


def test_filter_due_tables_keeps_never_synced_table():
    """No last_sync row → always due (matches is_table_due semantics)."""
    configs = [{"id": "new", "name": "new", "sync_schedule": "every 1h"}]
    repo = _FakeSyncStateRepo({})  # no entry at all
    out = filter_due_tables(configs, repo, now=_utc(2026, 5, 1, 10, 0))
    assert [c["id"] for c in out] == ["new"]


def test_filter_due_tables_treats_invalid_schedule_as_unscheduled():
    """Garbled sync_schedule: log + always sync (don't silently skip)."""
    configs = [{"id": "bad", "name": "bad", "sync_schedule": "BOGUS"}]
    repo = _FakeSyncStateRepo({"bad": _utc(2026, 5, 1, 9, 59)})
    out = filter_due_tables(configs, repo, now=_utc(2026, 5, 1, 10, 0))
    assert [c["id"] for c in out] == ["bad"]


def test_filter_due_tables_mixed_due_and_skipped():
    configs = [
        {"id": "due",     "name": "due",     "sync_schedule": "every 30m"},
        {"id": "skipped", "name": "skipped", "sync_schedule": "every 30m"},
        {"id": "always",  "name": "always",  "sync_schedule": None},
    ]
    repo = _FakeSyncStateRepo({
        "due":     _utc(2026, 5, 1, 9, 0),    # 60m ago → due
        "skipped": _utc(2026, 5, 1, 9, 50),   # 10m ago → skip
    })
    out = filter_due_tables(configs, repo, now=_utc(2026, 5, 1, 10, 0))
    assert sorted(c["id"] for c in out) == ["always", "due"]


def test_filter_due_tables_handles_naive_last_sync():
    """SyncStateRepository can return naive datetimes from older rows; helper
    must coerce to UTC instead of crashing on tz-aware vs naive comparison."""
    configs = [{"id": "old", "name": "old", "sync_schedule": "every 1h"}]
    naive_2h_ago = datetime(2026, 5, 1, 8, 0)  # no tzinfo
    repo = _FakeSyncStateRepo({"old": naive_2h_ago})
    out = filter_due_tables(configs, repo, now=_utc(2026, 5, 1, 10, 0))
    assert [c["id"] for c in out] == ["old"]
