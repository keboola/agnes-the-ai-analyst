"""Tests for TableConfig.sync_schedule field validation."""

import pytest

from src.config import TableConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_table(**overrides) -> TableConfig:
    """Create a TableConfig with sensible defaults, applying overrides."""
    defaults = dict(
        id="test.dataset.table",
        name="test_table",
        description="Test",
        primary_key="id",
        sync_strategy="full_refresh",
    )
    defaults.update(overrides)
    return TableConfig(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestSyncScheduleDefault:
    def test_default_is_none(self):
        table = _make_table()
        assert table.sync_schedule is None


class TestSyncScheduleValidValues:
    @pytest.mark.parametrize(
        "schedule",
        [
            "every 15m",
            "every 1h",
            "daily 05:00",
            "daily 07:00,13:00,18:00",
            "daily 00:00,12:00",
        ],
        ids=[
            "every-15m",
            "every-1h",
            "daily-single",
            "daily-three-times",
            "daily-two-times",
        ],
    )
    def test_valid_schedule_accepted(self, schedule: str):
        table = _make_table(sync_schedule=schedule)
        assert table.sync_schedule == schedule


class TestSyncScheduleEdgeCases:
    def test_every_zero_minutes(self):
        """every 0m matches the regex -- validation is syntactic, not semantic."""
        table = _make_table(sync_schedule="every 0m")
        assert table.sync_schedule == "every 0m"

    def test_daily_2359(self):
        table = _make_table(sync_schedule="daily 23:59")
        assert table.sync_schedule == "daily 23:59"


class TestSyncScheduleInvalid:
    @pytest.mark.parametrize(
        "bad_schedule",
        [
            "daily 07:00,13:00,18:00,",  # trailing comma
            "daily 7:00",                # single-digit hour
            "daily",                      # missing time
            "hourly",                     # unsupported keyword
            "weekly",                     # unsupported keyword
        ],
        ids=[
            "trailing-comma",
            "single-digit-hour",
            "daily-no-time",
            "hourly-keyword",
            "weekly-keyword",
        ],
    )
    def test_invalid_schedule_raises(self, bad_schedule: str):
        with pytest.raises(ValueError, match="Invalid sync_schedule"):
            _make_table(sync_schedule=bad_schedule)

    def test_empty_string_treated_as_none(self):
        """Empty string is falsy, so validation is skipped (same as None)."""
        table = _make_table(sync_schedule="")
        assert table.sync_schedule == ""

    def test_daily_25_accepted_by_regex(self):
        """25:00 passes regex validation (two digits). Document this behavior."""
        table = _make_table(sync_schedule="daily 25:00")
        assert table.sync_schedule == "daily 25:00"


class TestSyncScheduleNoneExplicit:
    def test_explicit_none_accepted(self):
        table = _make_table(sync_schedule=None)
        assert table.sync_schedule is None
