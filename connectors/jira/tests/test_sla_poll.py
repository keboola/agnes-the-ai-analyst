"""
Tests for connectors/jira/scripts/poll_sla.py - SLA polling and self-healing logic.

Covers:
- fetch_sla_and_status: API response parsing for SLA + status fields
- update_issue_sla: self-healing, skip logic, and missing JSON handling
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from connectors.jira.scripts.poll_sla import (
    SLA_FIELDS,
    STATUS_FIELDS,
    fetch_sla_and_status,
    update_issue_sla,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_issue_json_in_progress(tmp_path: Path) -> Path:
    """
    Create a temporary issues directory with a single issue JSON file
    whose status is "In Progress" (statusCategory "In Progress").
    Returns the raw_dir (parent of issues/).
    """
    issues_dir = tmp_path / "issues"
    issues_dir.mkdir()

    issue_data = {
        "key": "TEST-1",
        "fields": {
            "summary": "Test issue for SLA poll",
            "status": {
                "name": "In Progress",
                "statusCategory": {
                    "name": "In Progress",
                },
            },
            "resolution": None,
            "resolutiondate": None,
            "updated": "2026-01-15T10:00:00.000+0000",
            "customfield_10328": None,
            "customfield_10161": None,
        },
    }

    json_path = issues_dir / "TEST-1.json"
    json_path.write_text(json.dumps(issue_data, indent=2))
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: fetch_sla_and_status returns all 6 field types
# ---------------------------------------------------------------------------

class TestFetchSlaAndStatus:
    """Tests for the fetch_sla_and_status function."""

    @patch("connectors.jira.scripts.poll_sla.httpx.Client")
    def test_returns_all_sla_and_status_fields(self, mock_client_cls: MagicMock) -> None:
        """
        When the Jira API returns 200 with all requested fields,
        fetch_sla_and_status should return a dict containing every
        SLA_FIELD and STATUS_FIELD.
        """
        api_fields = {
            # SLA fields
            "customfield_10328": {
                "name": "Time to first response",
                "ongoingCycle": {
                    "elapsedTime": {"millis": 120000},
                    "remainingTime": {"millis": 600000},
                    "breached": False,
                },
            },
            "customfield_10161": {
                "name": "Time to resolution",
                "completedCycles": [],
                "ongoingCycle": {
                    "elapsedTime": {"millis": 360000},
                    "remainingTime": {"millis": 1440000},
                    "breached": False,
                },
            },
            # Status fields
            "status": {
                "name": "In Progress",
                "statusCategory": {"name": "In Progress"},
            },
            "resolution": None,
            "resolutiondate": None,
            "updated": "2026-02-18T14:30:00.000+0000",
        }

        # Build mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"fields": api_fields}

        mock_client_instance = MagicMock()
        mock_client_instance.get.return_value = mock_response
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client_instance

        result = fetch_sla_and_status(
            base_url="https://api.atlassian.com/ex/jira/fake-cloud-id/rest/api/3",
            auth=("user@example.com", "fake-token"),
            issue_key="TEST-1",
        )

        assert result is not None

        # All SLA fields must be present
        for field in SLA_FIELDS:
            assert field in result, f"SLA field {field} missing from result"

        # All STATUS fields must be present
        for field in STATUS_FIELDS:
            assert field in result, f"Status field {field} missing from result"

        # Verify specific values
        assert result["customfield_10328"]["name"] == "Time to first response"
        assert result["status"]["name"] == "In Progress"
        assert result["resolution"] is None
        assert result["updated"] == "2026-02-18T14:30:00.000+0000"


# ---------------------------------------------------------------------------
# Test 2: update_issue_sla self-healing
# ---------------------------------------------------------------------------

class TestUpdateIssueSlaHealing:
    """Tests for self-healing when API reports an issue as resolved."""

    @patch("connectors.jira.scripts.poll_sla.transform_single_issue")
    @patch("connectors.jira.scripts.poll_sla.fetch_sla_and_status")
    def test_self_healing_returns_healed_and_updates_json(
        self,
        mock_fetch: MagicMock,
        mock_transform: MagicMock,
        fake_issue_json_in_progress: Path,
    ) -> None:
        """
        Given a local JSON with status "In Progress",
        when the API says the issue is "Done" with resolution "Fixed",
        update_issue_sla should return "healed" and the JSON file
        should be updated with the new status fields.
        """
        raw_dir = fake_issue_json_in_progress

        # API returns resolved status
        mock_fetch.return_value = {
            "customfield_10328": {
                "name": "Time to first response",
                "completedCycles": [
                    {"elapsedTime": {"millis": 60000}, "breached": False}
                ],
            },
            "customfield_10161": {
                "name": "Time to resolution",
                "completedCycles": [
                    {"elapsedTime": {"millis": 300000}, "breached": False}
                ],
            },
            "status": {
                "name": "Done",
                "statusCategory": {"name": "Done"},
            },
            "resolution": {"name": "Fixed"},
            "resolutiondate": "2026-02-19T16:00:00.000+0000",
            "updated": "2026-02-19T16:00:01.000+0000",
        }
        mock_transform.return_value = True

        result = update_issue_sla(
            issue_key="TEST-1",
            raw_dir=raw_dir,
            base_url="https://api.atlassian.com/ex/jira/fake-cloud-id/rest/api/3",
            auth=("user@example.com", "fake-token"),
        )

        assert result == "healed"

        # Verify JSON was updated on disk
        updated_json_path = raw_dir / "issues" / "TEST-1.json"
        with open(updated_json_path) as f:
            updated_data = json.load(f)

        fields = updated_data["fields"]

        # Status should now reflect "Done"
        assert fields["status"]["statusCategory"]["name"] == "Done"
        assert fields["status"]["name"] == "Done"

        # Resolution should be set
        assert fields["resolution"]["name"] == "Fixed"
        assert fields["resolutiondate"] == "2026-02-19T16:00:00.000+0000"

        # SLA fields should be updated
        assert fields["customfield_10328"]["name"] == "Time to first response"
        assert fields["customfield_10161"]["name"] == "Time to resolution"

        # transform_single_issue should have been called once
        mock_transform.assert_called_once_with(issue_key="TEST-1")


# ---------------------------------------------------------------------------
# Test 3: update_issue_sla skips when no useful data
# ---------------------------------------------------------------------------

class TestUpdateIssueSlaSkip:
    """Tests for the skip logic when SLA data is empty and status is not Done."""

    @patch("connectors.jira.scripts.poll_sla.transform_single_issue")
    @patch("connectors.jira.scripts.poll_sla.fetch_sla_and_status")
    def test_skips_when_no_sla_data_and_not_resolved(
        self,
        mock_fetch: MagicMock,
        mock_transform: MagicMock,
        fake_issue_json_in_progress: Path,
    ) -> None:
        """
        When fetch_sla_and_status returns fields where SLA data is absent
        (null) and status is still not "Done", update_issue_sla should
        return "skipped" and NOT modify the JSON or call transform.
        """
        raw_dir = fake_issue_json_in_progress

        # API returns empty/null SLA data, status still In Progress
        mock_fetch.return_value = {
            "customfield_10328": None,
            "customfield_10161": None,
            "status": {
                "name": "In Progress",
                "statusCategory": {"name": "In Progress"},
            },
            "resolution": None,
            "resolutiondate": None,
            "updated": "2026-02-18T10:00:00.000+0000",
        }

        result = update_issue_sla(
            issue_key="TEST-1",
            raw_dir=raw_dir,
            base_url="https://api.atlassian.com/ex/jira/fake-cloud-id/rest/api/3",
            auth=("user@example.com", "fake-token"),
        )

        assert result == "skipped"

        # transform_single_issue should NOT have been called
        mock_transform.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: update_issue_sla returns "skipped" when JSON is missing
# ---------------------------------------------------------------------------

class TestUpdateIssueSlaJsonMissing:
    """Tests for missing JSON file handling."""

    @patch("connectors.jira.scripts.poll_sla.transform_single_issue")
    @patch("connectors.jira.scripts.poll_sla.fetch_sla_and_status")
    def test_returns_skipped_when_json_file_missing(
        self,
        mock_fetch: MagicMock,
        mock_transform: MagicMock,
        tmp_path: Path,
    ) -> None:
        """
        When the raw JSON file for the issue does not exist,
        update_issue_sla should return "skipped" immediately
        without calling the API or transform.
        """
        # Create the issues directory but no JSON file inside
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()

        result = update_issue_sla(
            issue_key="NONEXISTENT-999",
            raw_dir=tmp_path,
            base_url="https://api.atlassian.com/ex/jira/fake-cloud-id/rest/api/3",
            auth=("user@example.com", "fake-token"),
        )

        assert result == "skipped"

        # Should not have attempted to fetch or transform
        mock_fetch.assert_not_called()
        mock_transform.assert_not_called()
