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

import httpx

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


class TestPaginationFailureMarksIncomplete:
    """A page-fetch FAILURE must be distinguishable from a completed fetch.

    The incremental transform performs an issue-scoped delete-then-insert on
    the comments parquet, so overlaying a known-truncated list would delete
    previously stored rows. On failure, ``complete_issue_comments`` marks the
    issue with the ``_comments_incomplete`` sidecar key (a sibling of the
    ``_remote_links`` overlay-absent contract) so the transform preserves
    existing rows instead.
    """

    def test_non_200_page_marks_comments_incomplete(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        failing_response = MagicMock()
        failing_response.status_code = 500
        client = MagicMock()
        client.get.side_effect = [failing_response]

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert issue_data.get("_comments_incomplete") is True

    def test_request_error_marks_comments_incomplete(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = httpx.RequestError("connection dropped")

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert issue_data.get("_comments_incomplete") is True

    def test_successful_completion_sets_no_marker(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        extra_page = [_comment(f"extra{i}") for i in range(24)]
        client = _mock_client([extra_page])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert "_comments_incomplete" not in issue_data

    def test_under_cap_issue_sets_no_marker(self):
        issue_data = _issue_with_comments(total=42, embedded=42)
        client = _mock_client([])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert "_comments_incomplete" not in issue_data

    def test_stale_total_empty_page_sets_no_marker(self):
        """An empty page means the endpoint has no more comments — the fetched
        set IS complete relative to the endpoint; ``total`` was stale (e.g.
        comments deleted between the two requests). That is not a failure."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        empty_response = MagicMock()
        empty_response.status_code = 200
        empty_response.json.return_value = {"comments": []}
        client = MagicMock()
        client.get.side_effect = [empty_response]

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert "_comments_incomplete" not in issue_data


class TestPaginationDeduplicatesByCommentId:
    """The embed and GET /issue/{key}/comment are two separate requests; on
    ordering drift or deletions between them, ``startAt = len(embedded)`` can
    re-serve comments already embedded. Concatenation must de-duplicate by
    comment id (first occurrence wins, order preserved)."""

    def test_overlapping_page_produces_no_duplicate_ids(self):
        issue_data = _issue_with_comments(total=4, embedded=2)  # embeds c0, c1
        overlap_page = [_comment("c1"), _comment("c2"), _comment("c3")]
        client = _mock_client([overlap_page])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        comments = issue_data["fields"]["comment"]["comments"]
        ids = [c["id"] for c in comments]
        assert ids == ["c0", "c1", "c2", "c3"]

    def test_first_occurrence_wins_on_duplicate_id(self):
        issue_data = _issue_with_comments(total=3, embedded=2)  # embeds c0, c1
        embedded_c1 = issue_data["fields"]["comment"]["comments"][1]
        embedded_c1["body"] = {"type": "doc", "content": [], "marker": "embedded"}
        paged_c1 = _comment("c1")
        paged_c1["body"] = {"type": "doc", "content": [], "marker": "paged"}
        client = _mock_client([[paged_c1, _comment("c2")]])

        complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        comments = issue_data["fields"]["comment"]["comments"]
        c1 = next(c for c in comments if c["id"] == "c1")
        assert c1["body"].get("marker") == "embedded"


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


class TestFullRebuildEmitsPartialComments:
    """A ``_comments_incomplete`` issue must still contribute its partial
    comment list to the batch/full rebuild.

    The preserve-existing-rows contract only makes sense where the write is
    an issue-scoped delete-then-insert onto rows that are already there — the
    incremental path. ``transform_all`` rebuilds the monthly parquets from
    scratch, so returning ``None`` there means the issue contributes ZERO
    comment rows: strictly worse than writing the partially-fetched list the
    JSON already carries.
    """

    def test_transform_comments_emits_partial_list_for_full_rebuild(self):
        issue = _issue_with_comments(total=190, embedded=124)
        issue["_comments_incomplete"] = True

        records = transform_comments(issue, preserve_on_incomplete=False)

        assert records is not None
        assert len(records) == 124

    def test_transform_comments_default_preserves_for_incremental(self):
        """Default (incremental) behaviour is unchanged: None = skip the upsert."""
        issue = _issue_with_comments(total=190, embedded=124)
        issue["_comments_incomplete"] = True

        assert transform_comments(issue) is None

    def test_transform_all_writes_partial_comments_of_marked_issue(self, tmp_path):
        import json as _json

        from connectors.jira.transform import transform_all

        raw_dir = tmp_path / "raw"
        issues_dir = raw_dir / "issues"
        issues_dir.mkdir(parents=True)
        output_dir = tmp_path / "parquet"

        issue = _issue_with_comments(total=190, embedded=124, issue_key="PROJ-500")
        issue["_comments_incomplete"] = True
        issue["fields"]["summary"] = "marked incomplete"
        issue["fields"]["status"] = {"name": "Open"}
        issue["fields"]["issuetype"] = {"name": "Bug"}
        issue["fields"]["attachment"] = []
        issue["fields"]["created"] = "2026-05-15T00:00:00.000+0000"
        issue["fields"]["updated"] = "2026-05-15T00:00:00.000+0000"
        (issues_dir / "PROJ-500.json").write_text(_json.dumps(issue))

        counts = transform_all(raw_dir=raw_dir, output_dir=output_dir)

        assert counts["issues"] == 1
        assert counts["comments"] == 124, (
            "A full rebuild dropped every comment of an issue marked "
            "_comments_incomplete — the partial list in the JSON is strictly "
            "better than nothing when nothing is being preserved"
        )


class TestBackfillRefetchesIncompleteJson:
    """``--skip-existing`` must not make the marker permanent.

    ``process_issue`` skips any issue whose JSON already exists, so an issue
    marked ``_comments_incomplete`` by a failed pagination would never be
    re-fetched and never heal.
    """

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

    def _write_existing(self, backfill, issue_key: str, payload: dict) -> None:
        import json as _json

        backfill.issues_dir.mkdir(parents=True, exist_ok=True)
        (backfill.issues_dir / f"{issue_key}.json").write_text(_json.dumps(payload))

    def test_marked_json_is_refetched_under_skip_existing(self, tmp_path):
        backfill = self._make_backfill(tmp_path)
        marked = _issue_with_comments(total=190, embedded=124, issue_key="PROJ-600")
        marked["_comments_incomplete"] = True
        self._write_existing(backfill, "PROJ-600", marked)

        healed = _issue_with_comments(total=190, embedded=190, issue_key="PROJ-600")
        with (
            patch.object(backfill, "fetch_issue", return_value=healed) as fetch,
            patch.object(backfill, "fetch_remote_links", return_value=[]),
            patch.object(backfill, "download_issue_attachments", return_value=0),
        ):
            ok = backfill.process_issue("PROJ-600", skip_existing=True)

        assert ok is True
        fetch.assert_called_once_with("PROJ-600")
        assert backfill.stats["skipped"] == 0

    def test_unmarked_json_is_still_skipped(self, tmp_path):
        backfill = self._make_backfill(tmp_path)
        self._write_existing(backfill, "PROJ-601", _issue_with_comments(total=3, embedded=3, issue_key="PROJ-601"))

        with patch.object(backfill, "fetch_issue") as fetch:
            ok = backfill.process_issue("PROJ-601", skip_existing=True)

        assert ok is True
        fetch.assert_not_called()
        assert backfill.stats["skipped"] == 1

    def test_empty_json_is_refetched(self, tmp_path):
        """A zero-byte JSON is not evidence the issue was downloaded."""
        backfill = self._make_backfill(tmp_path)
        backfill.issues_dir.mkdir(parents=True, exist_ok=True)
        (backfill.issues_dir / "PROJ-602.json").write_text("")

        healed = _issue_with_comments(total=2, embedded=2, issue_key="PROJ-602")
        with (
            patch.object(backfill, "fetch_issue", return_value=healed) as fetch,
            patch.object(backfill, "fetch_remote_links", return_value=[]),
            patch.object(backfill, "download_issue_attachments", return_value=0),
        ):
            ok = backfill.process_issue("PROJ-602", skip_existing=True)

        assert ok is True
        fetch.assert_called_once_with("PROJ-602")

    def test_truncated_marked_json_is_refetched(self, tmp_path):
        """A payload that carries the marker but does not parse is re-fetched —
        the byte pre-filter admits it, and the confirming parse then fails."""
        backfill = self._make_backfill(tmp_path)
        backfill.issues_dir.mkdir(parents=True, exist_ok=True)
        (backfill.issues_dir / "PROJ-607.json").write_text('{"_comments_incomplete": true, "fields": { trunc')

        healed = _issue_with_comments(total=2, embedded=2, issue_key="PROJ-607")
        with (
            patch.object(backfill, "fetch_issue", return_value=healed) as fetch,
            patch.object(backfill, "fetch_remote_links", return_value=[]),
            patch.object(backfill, "download_issue_attachments", return_value=0),
        ):
            ok = backfill.process_issue("PROJ-607", skip_existing=True)

        assert ok is True
        fetch.assert_called_once_with("PROJ-607")

    def test_a_comment_quoting_the_marker_key_does_not_force_a_refetch(self, tmp_path):
        """The byte scan is a pre-filter, not the verdict: a comment body quoting
        the marker key must not turn every resume into a re-download of that issue."""
        backfill = self._make_backfill(tmp_path)
        payload = _issue_with_comments(total=1, embedded=1, issue_key="PROJ-608")
        payload["fields"]["comment"]["comments"][0]["body"] = 'we set "_comments_incomplete" on that one'
        self._write_existing(backfill, "PROJ-608", payload)

        with patch.object(backfill, "fetch_issue") as fetch:
            ok = backfill.process_issue("PROJ-608", skip_existing=True)

        assert ok is True
        fetch.assert_not_called()
        assert backfill.stats["skipped"] == 1


class TestCommentPaginationHonoursRetryAfter:
    """429 on a comment page is the likeliest failure in the batch path
    (parallel workers, an extra request per >100-comment issue). Without a
    retry, rate limiting routinely leaves issues marked incomplete. Mirror
    the surrounding fetchers: honour ``Retry-After``, bounded."""

    def _rate_limited(self, retry_after: str = "3") -> MagicMock:
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": retry_after}
        return resp

    def _page(self, comments: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"comments": comments}
        return resp

    def test_retries_after_429_and_completes(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [
            self._rate_limited("3"),
            self._page([_comment(f"extra{i}") for i in range(24)]),
        ]

        with patch("connectors.jira.service.time.sleep") as sleep:
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert len(issue_data["fields"]["comment"]["comments"]) == 124
        assert "_comments_incomplete" not in issue_data
        sleep.assert_called_once_with(3)

    def test_retry_is_bounded_and_marks_incomplete(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [self._rate_limited("1") for _ in range(20)]

        with patch("connectors.jira.service.time.sleep"):
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert issue_data.get("_comments_incomplete") is True
        assert client.get.call_count <= 5, "429 retry is unbounded"

    def test_unparseable_retry_after_falls_back_to_a_default_wait(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [
            self._rate_limited("Wed, 21 Oct 2026 07:28:00 GMT"),
            self._page([_comment(f"extra{i}") for i in range(24)]),
        ]

        with patch("connectors.jira.service.time.sleep") as sleep:
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert len(issue_data["fields"]["comment"]["comments"]) == 124
        sleep.assert_called_once()
        assert sleep.call_args.args[0] > 0

    def test_retry_after_wait_is_capped(self):
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [
            self._rate_limited("86400"),
            self._page([_comment(f"extra{i}") for i in range(24)]),
        ]

        with patch("connectors.jira.service.time.sleep") as sleep:
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        assert sleep.call_args.args[0] <= 300, "an hours-long Retry-After stalls the whole worker"


class TestWebhookFallbackPayloadIsNotAuthoritative:
    """When ``fetch_issue`` returns None the webhook's embedded issue payload
    is used as-is. It never passes through ``complete_issue_comments``, so its
    ``fields.comment.comments`` is whatever Jira chose to embed — and the
    incremental delete-then-insert would replace a complete stored thread with
    that shorter list. Mark it incomplete unless it demonstrably carries the
    whole thread."""

    def _make_service(self, tmp_path):
        from connectors.jira import service as svc

        svc.Config.JIRA_DOMAIN = "mycompany.atlassian.net"
        svc.Config.JIRA_EMAIL = "bot@mycompany.com"
        svc.Config.JIRA_API_TOKEN = "test-token-xyz"
        svc.Config.JIRA_DATA_DIR = tmp_path
        svc._jira_service = None
        return svc.JiraService()

    def _run_fallback(self, tmp_path, embedded_issue):
        service = self._make_service(tmp_path)
        saved: dict = {}

        def _capture(issue_data):
            saved["payload"] = issue_data
            return tmp_path / "issues" / "x.json"

        with (
            patch.object(service, "fetch_issue", return_value=None),
            patch.object(service, "save_issue", side_effect=_capture),
        ):
            ok = service.process_webhook_event({"webhookEvent": "jira:issue_updated", "issue": embedded_issue})
        assert ok is True
        return saved["payload"]

    def test_short_embedded_thread_is_marked_incomplete(self, tmp_path):
        embedded = _issue_with_comments(total=190, embedded=3, issue_key="PROJ-700")

        payload = self._run_fallback(tmp_path, embedded)

        assert payload.get("_comments_incomplete") is True

    def test_complete_embedded_thread_is_not_marked(self, tmp_path):
        embedded = _issue_with_comments(total=3, embedded=3, issue_key="PROJ-701")

        payload = self._run_fallback(tmp_path, embedded)

        assert "_comments_incomplete" not in payload

    def test_payload_without_comment_field_is_marked_incomplete(self, tmp_path):
        embedded = {"key": "PROJ-702", "id": "10002", "fields": {"summary": "no comment field"}}

        payload = self._run_fallback(tmp_path, embedded)

        assert payload.get("_comments_incomplete") is True, (
            "A payload carrying no comment field at all is not evidence the issue "
            "has no comments — the transform would delete the stored thread"
        )


class TestRateLimitWaitIsBudgeted:
    """``JIRA_COMMENT_RATE_LIMIT_RETRIES`` bounds the number of retries, not the
    wall-clock time they consume: three retries each honouring a 300s cap add up
    to 900s of ``time.sleep`` inside one pagination loop. The budget has to be
    total, not per-retry."""

    def _rate_limited(self, retry_after: str = "300") -> MagicMock:
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": retry_after}
        return resp

    def test_total_wait_is_bounded_across_the_whole_loop(self):
        issue_data = _issue_with_comments(total=1000, embedded=100)
        client = MagicMock()
        client.get.side_effect = [self._rate_limited("300") for _ in range(20)]

        with patch("connectors.jira.service.time.sleep") as sleep:
            complete_issue_comments(issue_data, BASE_URL, AUTH, client)

        slept = sum(call.args[0] for call in sleep.call_args_list)
        assert slept <= 300, (
            f"one pagination loop slept {slept}s in total — the retry cap bounds attempts, not wall time"
        )
        assert issue_data.get("_comments_incomplete") is True

    def test_caller_can_forbid_sleeping_entirely(self):
        """A request-serving caller has no budget to sleep at all: the marker
        plus a later heal is strictly better than parking the request."""
        issue_data = _issue_with_comments(total=124, embedded=100)
        client = MagicMock()
        client.get.side_effect = [self._rate_limited("60") for _ in range(5)]

        with patch("connectors.jira.service.time.sleep") as sleep:
            complete_issue_comments(issue_data, BASE_URL, AUTH, client, max_rate_limit_wait=0)

        sleep.assert_not_called()
        assert issue_data.get("_comments_incomplete") is True


class TestWebhookRefetchNeverSleeps:
    """``JiraService.fetch_issue`` is reached synchronously from
    ``process_webhook_event``, i.e. from a request the server is serving. It must
    not sleep out a rate limit there — the ``_comments_incomplete`` marker
    preserves the stored thread and the next webhook re-paginates."""

    def _make_service(self, jira_env):
        from connectors.jira import service as svc

        svc.Config.JIRA_DOMAIN = "mycompany.atlassian.net"
        svc.Config.JIRA_EMAIL = "bot@mycompany.com"
        svc.Config.JIRA_API_TOKEN = "test-token-xyz"
        svc.Config.JIRA_DATA_DIR = jira_env
        svc._jira_service = None
        return svc.JiraService()

    def test_rate_limited_comment_page_does_not_sleep_the_request(self, tmp_path):
        service = self._make_service(tmp_path)

        issue_response = MagicMock()
        issue_response.status_code = 200
        issue_response.json.return_value = _issue_with_comments(total=124, embedded=100, issue_key="PROJ-779")

        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "300"}

        client = MagicMock()
        client.get.side_effect = [issue_response] + [rate_limited] * 10
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)

        with (
            patch("connectors.jira.service.httpx.Client", return_value=client),
            patch("connectors.jira.service.time.sleep") as sleep,
        ):
            result = service.fetch_issue("PROJ-779")

        assert result is not None
        sleep.assert_not_called(), "the webhook path slept out a Jira rate limit while serving a request"
        assert result.get("_comments_incomplete") is True


class _TrackedClient:
    """httpx.Client stand-in that records how many are open simultaneously."""

    def __init__(self, tracker: dict, responder):
        self._tracker = tracker
        self._responder = responder

    def __enter__(self):
        self._tracker["open"] += 1
        self._tracker["max_open"] = max(self._tracker["max_open"], self._tracker["open"])
        return self

    def __exit__(self, *exc):
        self._tracker["open"] -= 1
        return False

    def get(self, *args, **kwargs):
        return self._responder()


class TestBackfillRateLimitRetryReleasesItsClient:
    """The 429 branch of ``JiraBackfill.fetch_issue`` retries from inside the
    enclosing ``with httpx.Client(...)``, so N consecutive rate limits hold N
    open clients (each with its own connection pool) and N stack frames — and
    the retry itself has no cap."""

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

    def _rate_limited(self) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "1"}
        return resp

    def test_retry_never_holds_two_clients_at_once(self, tmp_path):
        backfill = self._make_backfill(tmp_path)
        tracker = {"open": 0, "max_open": 0}

        ok = MagicMock()
        ok.status_code = 200
        ok.headers = {}
        ok.json.return_value = _issue_with_comments(total=3, embedded=3, issue_key="PROJ-700")
        queue = [self._rate_limited(), self._rate_limited(), ok]

        def factory(*args, **kwargs):
            response = queue.pop(0)
            return _TrackedClient(tracker, lambda: response)

        with (
            patch("connectors.jira.scripts.backfill.httpx.Client", side_effect=factory),
            patch("connectors.jira.scripts.backfill.time.sleep"),
        ):
            result = backfill.fetch_issue("PROJ-700")

        assert result is not None
        assert tracker["max_open"] == 1, (
            f"{tracker['max_open']} httpx clients were open at once — the rate-limit "
            f"retry runs inside the previous client's context manager"
        )
        assert tracker["open"] == 0

    def test_retry_is_bounded(self, tmp_path):
        backfill = self._make_backfill(tmp_path)
        tracker = {"open": 0, "max_open": 0}
        created = []

        def factory(*args, **kwargs):
            client = _TrackedClient(tracker, self._rate_limited)
            created.append(client)
            return client

        with (
            patch("connectors.jira.scripts.backfill.httpx.Client", side_effect=factory),
            patch("connectors.jira.scripts.backfill.time.sleep"),
        ):
            result = backfill.fetch_issue("PROJ-701")

        assert result is None
        assert len(created) <= 5, f"a permanently rate-limited issue retried {len(created)} times — unbounded"


class TestSkipExistingStaysCheap:
    """``--skip-existing`` defaults to True and exists for resuming a run that
    already holds most of the corpus. Deciding to skip must not fully parse each
    stored payload: these are ``fields=*all`` + ``expand=renderedFields,changelog``
    documents with every comment, so a resumed six-figure run would parse tens of
    GB purely to decide to skip."""

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

    def _write_existing(self, backfill, issue_key: str, payload: dict) -> None:
        import json as _json

        backfill.issues_dir.mkdir(parents=True, exist_ok=True)
        (backfill.issues_dir / f"{issue_key}.json").write_text(_json.dumps(payload))

    def test_skipping_a_complete_issue_does_not_parse_its_payload(self, tmp_path):
        import connectors.jira.scripts.backfill as backfill_mod

        backfill = self._make_backfill(tmp_path)
        self._write_existing(backfill, "PROJ-603", _issue_with_comments(total=3, embedded=3, issue_key="PROJ-603"))

        boom = AssertionError("the skip decision parsed the whole stored issue payload")
        with (
            patch.object(backfill_mod.json, "load", side_effect=boom),
            patch.object(backfill_mod.json, "loads", side_effect=boom),
            patch.object(backfill, "fetch_issue") as fetch,
        ):
            ok = backfill.process_issue("PROJ-603", skip_existing=True)

        assert ok is True
        fetch.assert_not_called()
        assert backfill.stats["skipped"] == 1

    def test_dry_run_counts_the_same_issues_a_real_run_would_skip(self, tmp_path):
        """``--dry-run`` counted `existing` with a bare ``.exists()`` while a real
        run re-fetches anything marked incomplete, so dry-run under-reported what
        the download would actually do. Both must ask the same predicate."""
        from connectors.jira.scripts.backfill import would_skip_issue

        backfill = self._make_backfill(tmp_path)

        marked = _issue_with_comments(total=190, embedded=124, issue_key="PROJ-604")
        marked["_comments_incomplete"] = True
        self._write_existing(backfill, "PROJ-604", marked)
        self._write_existing(backfill, "PROJ-605", _issue_with_comments(total=3, embedded=3, issue_key="PROJ-605"))

        assert would_skip_issue(backfill.issues_dir, "PROJ-604") is False
        assert would_skip_issue(backfill.issues_dir, "PROJ-605") is True
        assert would_skip_issue(backfill.issues_dir, "PROJ-606") is False
