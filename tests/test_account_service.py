"""Tests for webapp.account_service module."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from webapp.account_service import (
    _get_enabled_datasets,
    _get_last_sync,
    _get_notification_scripts,
    _get_server_username,
    _humanize_cron,
    _parse_cron_schedule,
    get_account_details,
)


class TestHumanizeCron:
    """Test cron expression to human-readable conversion."""

    def test_every_5_minutes(self):
        assert _humanize_cron("*/5 * * * *") == "Every 5 minutes"

    def test_every_minute_star(self):
        assert _humanize_cron("* * * * *") == "Every minute"

    def test_every_1_minute(self):
        assert _humanize_cron("*/1 * * * *") == "Every minute"

    def test_every_30_minutes(self):
        assert _humanize_cron("*/30 * * * *") == "Every 30 minutes"

    def test_every_hour_at_specific_minute(self):
        assert _humanize_cron("0 * * * *") == "Every hour"

    def test_every_2_hours(self):
        assert _humanize_cron("0 */2 * * *") == "Every 2 hours"

    def test_every_1_hour_explicit(self):
        assert _humanize_cron("0 */1 * * *") == "Every hour"

    def test_daily_at_time(self):
        assert _humanize_cron("0 9 * * *") == "Daily at 09:00"

    def test_daily_midnight(self):
        assert _humanize_cron("0 0 * * *") == "Daily at 00:00"

    def test_complex_fallback(self):
        # Complex expressions return raw string
        expr = "0 9 1 * *"
        assert _humanize_cron(expr) == expr

    def test_invalid_parts(self):
        assert _humanize_cron("invalid") == "invalid"

    def test_hourly_specific_minute(self):
        assert _humanize_cron("30 * * * *") == "Every hour"


class TestParseCronSchedule:
    """Test crontab output parsing."""

    def test_standard_crontab(self):
        output = "*/5 * * * * /home/user/.venv/bin/python /home/user/run.py\n"
        assert _parse_cron_schedule(output) == "Every 5 minutes"

    def test_with_comments(self):
        output = "# m h dom mon dow command\n*/10 * * * * /usr/bin/some-cmd\n"
        assert _parse_cron_schedule(output) == "Every 10 minutes"

    def test_empty_crontab(self):
        assert _parse_cron_schedule("") is None

    def test_only_comments(self):
        assert _parse_cron_schedule("# just a comment\n") is None

    def test_multiple_entries_returns_first(self):
        output = "*/5 * * * * cmd1\n0 9 * * * cmd2\n"
        assert _parse_cron_schedule(output) == "Every 5 minutes"


class TestGetServerUsername:
    """Test webapp-to-server username mapping."""

    @patch("webapp.account_service.WEBAPP_TO_SERVER_USERNAME", {"john.doe": "john"})
    def test_mapped_user(self):
        assert _get_server_username("john.doe") == "john"

    def test_unmapped_user(self):
        assert _get_server_username("jane.smith") == "jane.smith"


class TestGetNotificationScripts:
    """Test fetching notification scripts via subprocess."""

    @patch("webapp.account_service.subprocess.run")
    def test_success(self, mock_run):
        scripts = [
            {"name": "data_freshness.py", "stem": "data_freshness", "last_run": "2h ago"}
        ]
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(scripts)
        )
        result = _get_notification_scripts("testuser")
        assert len(result) == 1
        assert result[0]["stem"] == "data_freshness"

    @patch("webapp.account_service.subprocess.run")
    def test_failure_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        result = _get_notification_scripts("testuser")
        assert result == []

    @patch("webapp.account_service.subprocess.run")
    def test_timeout_returns_empty(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=10)
        result = _get_notification_scripts("testuser")
        assert result == []

    @patch("webapp.account_service.subprocess.run")
    def test_invalid_json_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json")
        result = _get_notification_scripts("testuser")
        assert result == []


class TestGetLastSync:
    """Test fetching last sync status."""

    @patch("webapp.account_service.subprocess.run")
    def test_synced(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "synced": True,
                "elapsed_seconds": 7200,
                "elapsed_display": "2h ago",
            }),
        )
        assert _get_last_sync("testuser") == "2h ago"

    @patch("webapp.account_service.subprocess.run")
    def test_never_synced(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"synced": False, "elapsed_seconds": None}),
        )
        assert _get_last_sync("testuser") is None

    @patch("webapp.account_service.subprocess.run")
    def test_command_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        assert _get_last_sync("testuser") is None


class TestGetAccountDetails:
    """Test the main get_account_details function."""

    @patch("webapp.account_service._get_enabled_datasets")
    @patch("webapp.account_service._get_last_sync")
    @patch("webapp.account_service._get_cron_schedule")
    @patch("webapp.account_service._get_notification_scripts")
    def test_full_details(self, mock_scripts, mock_cron, mock_sync, mock_datasets):
        mock_scripts.return_value = [
            {"name": "test.py", "stem": "test", "last_run": "1h ago"}
        ]
        mock_cron.return_value = "Every 5 minutes"
        mock_sync.return_value = "3h ago"
        mock_datasets.return_value = ["jira"]

        result = get_account_details("testuser")
        assert result is not None
        assert result["script_count"] == 1
        assert result["cron_schedule"] == "Every 5 minutes"
        assert result["last_sync_display"] == "3h ago"
        assert result["sync_datasets_enabled"] == ["jira"]

    def test_invalid_username_returns_none(self):
        assert get_account_details("") is None
        assert get_account_details("INVALID") is None
        assert get_account_details("root; rm -rf /") is None

    @patch("webapp.account_service._get_enabled_datasets")
    @patch("webapp.account_service._get_last_sync")
    @patch("webapp.account_service._get_cron_schedule")
    @patch("webapp.account_service._get_notification_scripts")
    def test_no_scripts_no_cron_no_sync(self, mock_scripts, mock_cron, mock_sync, mock_datasets):
        mock_scripts.return_value = []
        mock_cron.return_value = None
        mock_sync.return_value = None
        mock_datasets.return_value = []

        result = get_account_details("newuser")
        assert result is not None
        assert result["script_count"] == 0
        assert result["cron_schedule"] is None
        assert result["last_sync_display"] is None
        assert result["sync_datasets_enabled"] == []

    @patch("webapp.account_service.WEBAPP_TO_SERVER_USERNAME", {"john.doe": "john"})
    @patch("webapp.account_service._get_enabled_datasets")
    @patch("webapp.account_service._get_last_sync")
    @patch("webapp.account_service._get_cron_schedule")
    @patch("webapp.account_service._get_notification_scripts")
    def test_username_mapping(self, mock_scripts, mock_cron, mock_sync, mock_datasets):
        mock_scripts.return_value = []
        mock_cron.return_value = None
        mock_sync.return_value = None
        mock_datasets.return_value = []

        get_account_details("john.doe")
        # Verify server username mapping: john.doe -> john
        mock_scripts.assert_called_once_with("john")
        mock_cron.assert_called_once_with("john")
        mock_sync.assert_called_once_with("john")
