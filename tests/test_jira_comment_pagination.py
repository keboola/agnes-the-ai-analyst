"""Tests for Jira comment-pagination completion (issue #1257).

Jira's issue endpoint embeds ``fields.comment.comments`` capped at 100,
oldest-first. An issue with more than 100 comments therefore arrives missing
its NEWEST comments unless the fetch layer pages through the comment
endpoint for the remainder — and because every later full-refetch
(``fields=*all``) re-hits the same 100-comment cap, the gap never heals on
its own.

Covers both ingestion paths that share the ``complete_issue_comments`` fetch
seam: the batch/full extract (``JiraBackfill.fetch_issue``) and the webhook
full-refetch (``JiraService.fetch_issue``).
"""

from unittest.mock import MagicMock, patch


from connectors.jira.service import complete_issue_comments
from connectors.jira.transform import transform_comments, transform_issue

BASE_URL = "https://mycompany.atlassian.net/rest/api/3"
AUTH = ("bot@mycompany.com", "test-token")


def _comment(comment_id: str) -> dict:
    return {
        "id": comment_id,
        "author": {"emailAddress": f"{comment_id}@example.com", "displayName": comment_id},
        "updateAuthor": {"emailAddress": f"{comment_id}@example.com", "displayName": comment_id},
        "body": {"type": "doc", "content": []},
        "created": "2026-01-01T00:00:00.000+0000",
        "updated": "2026-01-01T00:00:00.000+0000",
    }


def _issue_with_comments(total: int, embedded: int, issue_key: str = "PROJ-1") -> dict:
    return {
        "key": issue_key,
        "id": "10001",
        "fields": {
            "comment": {
                "total": total,
                "comments": [_comment(f"c{i}") for i in range(embedded)],
            }
        },
    }


def _mock_client(pages: list[list[dict]] | None = None, status_code: int = 200) -> MagicMock:
    """MagicMock httpx.Client whose .get() returns successive comment pages."""
    client = MagicMock()
    if pages is None:
        responses = []
    else:
        responses = []
        for page in pages:
            resp = MagicMock()
            resp.status_code = status_code
            resp.json.return_value = {"comments": page}
            responses.append(resp)
    client.get.side_effect = responses
    return client


class TestCompleteIssueComments:
    """Unit tests for the shared fetch-layer completion helper."""

    def test_pages_through_remaining_comments(self):
        """total=124, 100 embedded -> ONE paginated call fetches the remaining 24."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        extra_page = [_comment(f"extra{i}") for i in range(24)]
        client = _mock_client([extra_page])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        comments = issue_data["fields"]["comment"]["comments"]
        assert len(comments) == 124
        client.get.assert_called_once()
        _, kwargs = client.get.call_args
        assert kwargs["params"]["startAt"] == 100
        assert kwargs["params"]["maxResults"] == 100

    def test_under_cap_issues_no_extra_http_call(self):
        """total == embedded (issue under the 100-comment page cap) -> no extra HTTP call."""
        issue_data = _issue_with_comments(total=42, embedded=42)
        client = _mock_client([])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        client.get.assert_not_called()
        assert len(issue_data["fields"]["comment"]["comments"]) == 42

    def test_transform_stores_full_comment_set_and_comment_count(self):
        """After completion, transform_issue/transform_comments see the full set."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        extra_page = [_comment(f"extra{i}") for i in range(24)]
        client = _mock_client([extra_page])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert transform_issue(issue_data)["comment_count"] == 124
        assert len(transform_comments(issue_data)) == 124

    def test_pages_multiple_times_when_gap_exceeds_one_page(self):
        """total=250, 100 embedded -> two paginated calls (100 + 50)."""
        issue_data = _issue_with_comments(total=250, embedded=100)
        page1 = [_comment(f"p1-{i}") for i in range(100)]
        page2 = [_comment(f"p2-{i}") for i in range(50)]
        client = _mock_client([page1, page2])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert len(issue_data["fields"]["comment"]["comments"]) == 250
        assert client.get.call_count == 2
        first_call_start_at = client.get.call_args_list[0].kwargs["params"]["startAt"]
        second_call_start_at = client.get.call_args_list[1].kwargs["params"]["startAt"]
        assert first_call_start_at == 100
        assert second_call_start_at == 200

    def test_logs_warning_when_still_short_after_completion(self, caplog):
        """A page-fetch failure leaves stored < total -> WARNING, not an exception."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        failing_response = MagicMock()
        failing_response.status_code = 500
        client = MagicMock()
        client.get.side_effect = [failing_response]

        with caplog.at_level("WARNING"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert len(issue_data["fields"]["comment"]["comments"]) == 100
        assert any("comment.total" in record.message for record in caplog.records)

    def test_no_warning_when_completion_succeeds(self, caplog):
        issue_data = _issue_with_comments(total=124, embedded=100)
        extra_page = [_comment(f"extra{i}") for i in range(24)]
        client = _mock_client([extra_page])

        with caplog.at_level("WARNING"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert not any("comment.total" in record.message for record in caplog.records)


class TestServiceFetchIssuePaginates:
    """The webhook full-refetch path (JiraService.fetch_issue) gets the same completion."""

    def _make_service(self, jira_env):
        from connectors.jira import service as svc

        svc.Config.JIRA_DOMAIN = "mycompany.atlassian.net"
        svc.Config.JIRA_EMAIL = "bot@mycompany.com"
        svc.Config.JIRA_API_TOKEN = "test-token-xyz"
        svc.Config.JIRA_DATA_DIR = jira_env
        svc._jira_service = None
        return svc.JiraService()

    def test_webhook_full_refetch_completes_truncated_comments(self, tmp_path):
        service = self._make_service(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=124, embedded=100, issue_key="PROJ-777")

        page_response = MagicMock()
        page_response.status_code = 200
        page_response.json.return_value = {"comments": [_comment(f"extra{i}") for i in range(24)]}

        client = MagicMock()
        client.get.side_effect = [issue_response, page_response]
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.service.httpx.Client", return_value=client):
            result = service.fetch_issue("PROJ-777")

        assert result is not None
        assert len(result["fields"]["comment"]["comments"]) == 124
        assert client.get.call_count == 2

    def test_webhook_full_refetch_under_cap_makes_single_call(self, tmp_path):
        service = self._make_service(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=5, embedded=5, issue_key="PROJ-778")

        client = MagicMock()
        client.get.side_effect = [issue_response]
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.service.httpx.Client", return_value=client):
            result = service.fetch_issue("PROJ-778")

        assert result is not None
        assert len(result["fields"]["comment"]["comments"]) == 5
        client.get.assert_called_once()


class TestBackfillFetchIssuePaginates:
    """The batch/full extract path (JiraBackfill.fetch_issue) gets the same completion."""

    def _make_backfill(self, tmp_path):
        from connectors.jira.scripts.backfill import Config as BackfillConfig
        from connectors.jira.scripts.backfill import JiraBackfill

        config = BackfillConfig(
            jira_domain="mycompany.atlassian.net",
            jira_email="bot@mycompany.com",
            jira_api_token="test-token-xyz",
            data_dir=tmp_path,
        )
        return JiraBackfill(config)

    def test_batch_full_extract_completes_truncated_comments(self, tmp_path):
        backfill = self._make_backfill(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=124, embedded=100, issue_key="PROJ-999")

        page_response = MagicMock()
        page_response.status_code = 200
        page_response.json.return_value = {"comments": [_comment(f"extra{i}") for i in range(24)]}

        client = MagicMock()
        client.get.side_effect = [issue_response, page_response]
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.scripts.backfill.httpx.Client", return_value=client):
            result = backfill.fetch_issue("PROJ-999")

        assert result is not None
        assert len(result["fields"]["comment"]["comments"]) == 124
        assert client.get.call_count == 2

    def test_batch_full_extract_under_cap_makes_single_call(self, tmp_path):
        backfill = self._make_backfill(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=3, embedded=3, issue_key="PROJ-1000")

        client = MagicMock()
        client.get.side_effect = [issue_response]
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        with patch("connectors.jira.scripts.backfill.httpx.Client", return_value=client):
            result = backfill.fetch_issue("PROJ-1000")

        assert result is not None
        assert len(result["fields"]["comment"]["comments"]) == 3
        client.get.assert_called_once()
