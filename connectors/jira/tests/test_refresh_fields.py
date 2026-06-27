"""
Tests for the generic single-token field-refresh path
(spec 2026-06-23-jira-sla-single-token / generic variant).

Covers:
- refresh_fields(): parse JIRA_REFRESH_FIELDS (id or id:alias, validated)
- JiraService.fetch_refresh_fields(): primary token + domain/gateway URL, configured ids
- JiraService.save_issue(): overlay writes configured fields, skips when unset
- transform_issue(): one JSON column per configured field; absent -> null; no flat SLA cols
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from connectors.jira import service as jira_service
from connectors.jira.service import JiraService, refresh_fields
from connectors.jira.transform import (
    ISSUES_SCHEMA,
    REFRESH_COLLISION_PREFIX,
    resolved_refresh_columns,
    transform_issue,
)

REFRESH_ENV = "JIRA_REFRESH_FIELDS"


@pytest.fixture()
def clear_refresh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REFRESH_ENV, raising=False)


# ---------------------------------------------------------------------------
# refresh_fields() — parse JIRA_REFRESH_FIELDS
# ---------------------------------------------------------------------------


class TestRefreshFields:
    def test_empty_when_unset(self, clear_refresh_env: None) -> None:
        assert refresh_fields() == []

    def test_id_only_defaults_column_to_id(self, clear_refresh_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_1")
        assert refresh_fields() == [("customfield_1", "customfield_1")]

    def test_id_with_alias(self, clear_refresh_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_1:first_response")
        assert refresh_fields() == [("customfield_1", "first_response")]

    def test_multiple_mixed_and_whitespace(self, clear_refresh_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REFRESH_ENV, " a:x , b , c:z ")
        assert refresh_fields() == [("a", "x"), ("b", "b"), ("c", "z")]

    def test_invalid_alias_falls_back_to_id(self, clear_refresh_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        # Alias starting with a digit is not a valid column name -> use the id.
        monkeypatch.setenv(REFRESH_ENV, "customfield_1:1bad")
        assert refresh_fields() == [("customfield_1", "customfield_1")]

    def test_empty_entries_skipped(self, clear_refresh_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REFRESH_ENV, "a,,b,")
        assert refresh_fields() == [("a", "a"), ("b", "b")]


# ---------------------------------------------------------------------------
# fetch_refresh_fields() — primary token, domain/gateway, configured ids
# ---------------------------------------------------------------------------


def _mock_client(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body if json_body is not None else {}
    client = MagicMock()
    client.get.return_value = response
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


def _called_url(client: MagicMock) -> str:
    args, kwargs = client.get.call_args
    return args[0] if args else kwargs["url"]


@pytest.fixture()
def configured_service(monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None) -> JiraService:
    monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "", raising=False)
    svc = JiraService()
    svc.domain = "mycompany.atlassian.net"
    svc.email = "bot@mycompany.com"
    svc.api_token = "tok-123"
    return svc


class TestFetchRefreshFields:
    def test_none_when_no_fields_configured(self, configured_service: JiraService) -> None:
        with patch.object(jira_service.httpx, "Client") as mock_cls:
            assert configured_service.fetch_refresh_fields("SUPPORT-1") is None
        mock_cls.assert_not_called()

    def test_primary_auth_domain_url_configured_ids(
        self, configured_service: JiraService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_1:first_response,customfield_2")
        client = _mock_client(200, {"fields": {"customfield_1": {"name": "FR"}}})
        with patch.object(jira_service.httpx, "Client", return_value=client):
            result = configured_service.fetch_refresh_fields("SUPPORT-1")
        assert result == {"customfield_1": {"name": "FR"}}
        _, kwargs = client.get.call_args
        assert _called_url(client) == "https://mycompany.atlassian.net/rest/api/3/issue/SUPPORT-1"
        assert kwargs["auth"] == ("bot@mycompany.com", "tok-123")
        assert kwargs["params"]["fields"] == "customfield_1,customfield_2"

    def test_gateway_url_when_cloud_id_set(
        self, configured_service: JiraService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_1")
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        client = _mock_client(200, {"fields": {}})
        with patch.object(jira_service.httpx, "Client", return_value=client):
            configured_service.fetch_refresh_fields("SUPPORT-1")
        assert _called_url(client) == "https://api.atlassian.com/ex/jira/cloud-xyz/rest/api/3/issue/SUPPORT-1"

    def test_none_when_service_unconfigured(self, monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_1")
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "", raising=False)
        svc = JiraService()
        svc.domain = ""
        svc.email = ""
        svc.api_token = ""
        with patch.object(jira_service.httpx, "Client") as mock_cls:
            assert svc.fetch_refresh_fields("SUPPORT-1") is None
        mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# save_issue() overlay
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None) -> JiraService:
    data_dir = tmp_path / "jira_data"
    (data_dir / "issues").mkdir(parents=True)
    monkeypatch.setattr(jira_service.Config, "JIRA_DOMAIN", "mycompany.atlassian.net", raising=False)
    monkeypatch.setattr(jira_service.Config, "JIRA_EMAIL", "bot@mycompany.com", raising=False)
    monkeypatch.setattr(jira_service.Config, "JIRA_API_TOKEN", "tok-123", raising=False)
    monkeypatch.setattr(jira_service.Config, "JIRA_DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "", raising=False)
    return JiraService()


class TestSaveIssueOverlay:
    def test_overlay_writes_configured_fields(self, tmp_service: JiraService, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_99001:lunch")
        fetched = {"customfield_99001": {"value": "pizza"}}
        issue_data = {"key": "SUPPORT-1", "fields": {"summary": "x"}}
        with (
            patch.object(tmp_service, "fetch_remote_links", return_value=[]),
            patch.object(tmp_service, "fetch_refresh_fields", return_value=fetched),
            patch.object(tmp_service, "download_all_attachments", return_value=[]),
            patch("connectors.jira.service.trigger_incremental_transform", return_value=True),
        ):
            path = tmp_service.save_issue(issue_data)
        assert path is not None
        fields = json.loads(Path(path).read_text())["fields"]
        assert fields["customfield_99001"] == {"value": "pizza"}

    def test_no_overlay_when_fetch_none(self, tmp_service: JiraService, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_99001")
        issue_data = {"key": "SUPPORT-2", "fields": {"summary": "x"}}
        with (
            patch.object(tmp_service, "fetch_remote_links", return_value=[]),
            patch.object(tmp_service, "fetch_refresh_fields", return_value=None),
            patch.object(tmp_service, "download_all_attachments", return_value=[]),
            patch("connectors.jira.service.trigger_incremental_transform", return_value=True),
        ):
            path = tmp_service.save_issue(issue_data)
        fields = json.loads(Path(path).read_text())["fields"]
        assert "customfield_99001" not in fields


# ---------------------------------------------------------------------------
# transform_issue() — JSON column per configured field
# ---------------------------------------------------------------------------


class TestTransformGenericColumns:
    @staticmethod
    def _raw(fields: dict) -> dict:
        return {"key": "SUPPORT-1", "id": "1", "fields": fields}

    def test_emits_json_column_with_alias(self, monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_99001:lunch")
        rec = transform_issue(self._raw({"customfield_99001": {"value": "pizza"}}))
        assert rec["lunch"] == json.dumps({"value": "pizza"})

    def test_absent_field_is_null(self, monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_99001:lunch")
        rec = transform_issue(self._raw({"summary": "x"}))
        assert rec["lunch"] is None

    def test_no_flat_sla_columns(self, monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None) -> None:
        rec = transform_issue(self._raw({"summary": "x"}))
        assert "first_response_elapsed_millis" not in rec
        assert "time_to_resolution_elapsed_millis" not in rec


# ---------------------------------------------------------------------------
# Column-name collision guard (clean names; prefix only on collision)
# ---------------------------------------------------------------------------


class TestRefreshColumnCollision:
    @staticmethod
    def _raw(fields: dict) -> dict:
        return {"key": "SUPPORT-1", "id": "1", "fields": fields}

    def test_alias_colliding_with_builtin_is_prefixed_builtin_preserved(
        self, monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None
    ) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_99001:resolution")
        rec = transform_issue(
            self._raw(
                {
                    "resolution": {"name": "Fixed"},
                    "customfield_99001": {"name": "Time to resolution", "ongoingCycle": {}},
                }
            )
        )
        # the built-in `resolution` column keeps its clean extracted value
        assert rec["resolution"] == "Fixed"
        # the refresh field lands under the prefixed column, not over the built-in
        assert json.loads(rec["cf_resolution"]) == {"name": "Time to resolution", "ongoingCycle": {}}

    def test_standard_field_key_collision_no_alias(
        self, monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None
    ) -> None:
        # Refreshing the standard `resolution` field with no alias -> column would
        # be "resolution" (== built-in) -> must be prefixed; built-in preserved.
        monkeypatch.setenv(REFRESH_ENV, "resolution")
        rec = transform_issue(self._raw({"resolution": {"name": "Fixed"}}))
        assert rec["resolution"] == "Fixed"
        assert json.loads(rec["cf_resolution"]) == {"name": "Fixed"}

    def test_duplicate_columns_first_wins(self, monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_1:dup,customfield_2:dup")
        rec = transform_issue(self._raw({"customfield_1": {"a": 1}, "customfield_2": {"b": 2}}))
        assert json.loads(rec["dup"]) == {"a": 1}

    def test_non_colliding_alias_stays_clean(self, monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_99001:lunch")
        rec = transform_issue(self._raw({"customfield_99001": {"value": "pizza"}}))
        assert "lunch" in rec
        assert "cf_lunch" not in rec

    def test_resolved_refresh_columns(self, monkeypatch: pytest.MonkeyPatch, clear_refresh_env: None) -> None:
        monkeypatch.setenv(REFRESH_ENV, "customfield_1:status,customfield_2:lunch,customfield_3:lunch")
        # `status` collides -> cf_status; lunch clean; the 2nd lunch is skipped
        assert resolved_refresh_columns() == [
            ("customfield_1", "cf_status"),
            ("customfield_2", "lunch"),
        ]


class TestRefreshNamespaceLock:
    def test_no_builtin_starts_with_collision_prefix(self) -> None:
        assert not any(k.startswith(REFRESH_COLLISION_PREFIX) for k in ISSUES_SCHEMA)
