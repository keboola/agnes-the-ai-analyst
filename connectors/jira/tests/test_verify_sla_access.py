"""
Tests for connectors/jira/scripts/verify_sla_access.py — the live field preflight.

The preflight is operator-run against the real API; these tests mock httpx so the
discovery/classification/exit-code logic is verified deterministically, including
the guarantee that secrets never appear in rendered output.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from connectors.jira.scripts import verify_sla_access as v


def _mock_client(status_code: int = 200, json_body=None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body if json_body is not None else {}
    client = MagicMock()
    client.get.return_value = response
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


@pytest.fixture()
def primary_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_DOMAIN", "x.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "e@x.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret-tok")
    for var in ("JIRA_CLOUD_ID", "JIRA_REFRESH_FIELDS"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# classify_field — present / permission-error / null
# ---------------------------------------------------------------------------


class TestClassifyField:
    def test_present_value(self) -> None:
        assert v.classify_field({"name": "FR", "ongoingCycle": {}}) == "present"
        assert v.classify_field("pizza") == "present"

    def test_permission_error(self) -> None:
        assert v.classify_field({"errorMessage": "no permission"}) == "permission-error"

    def test_null(self) -> None:
        assert v.classify_field(None) == "null"


# ---------------------------------------------------------------------------
# build_base_urls — domain always, gateway when cloud_id set
# ---------------------------------------------------------------------------


class TestBuildBaseUrls:
    def test_domain_only(self) -> None:
        assert v.build_base_urls("x.atlassian.net", "") == [("domain", "https://x.atlassian.net/rest/api/3")]

    def test_with_gateway(self) -> None:
        urls = v.build_base_urls("x.atlassian.net", "cloud-1")
        assert urls[0] == ("domain", "https://x.atlassian.net/rest/api/3")
        assert ("gateway", "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3") in urls


# ---------------------------------------------------------------------------
# list_custom_fields — all custom fields (id, name, type)
# ---------------------------------------------------------------------------


class TestListCustomFields:
    def test_returns_custom_fields_only(self) -> None:
        body = [
            {
                "id": "customfield_1",
                "name": "Time to first response",
                "schema": {"custom": "com.atlassian.servicedesk:sd-sla-field"},
            },
            {
                "id": "customfield_2",
                "name": "Lunch",
                "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:textfield"},
            },
            {"id": "summary", "name": "Summary"},  # not a custom field
        ]
        client = _mock_client(200, body)
        with patch.object(v.httpx, "Client", return_value=client):
            fields = v.list_custom_fields("https://x.atlassian.net/rest/api/3", ("e@x.com", "tok"))
        assert fields == [
            {
                "id": "customfield_1",
                "name": "Time to first response",
                "type": "com.atlassian.servicedesk:sd-sla-field",
            },
            {
                "id": "customfield_2",
                "name": "Lunch",
                "type": "com.atlassian.jira.plugin.system.customfieldtypes:textfield",
            },
        ]


# ---------------------------------------------------------------------------
# check_issue — per-field classification
# ---------------------------------------------------------------------------


class TestCheckIssue:
    def test_classifies_each_field(self) -> None:
        body = {
            "fields": {
                "customfield_1": {"value": "pizza"},
                "customfield_2": {"errorMessage": "no permission"},
            }
        }
        client = _mock_client(200, body)
        with patch.object(v.httpx, "Client", return_value=client):
            res = v.check_issue(
                "https://x.atlassian.net/rest/api/3",
                ("e@x.com", "tok"),
                "SUPPORT-1",
                ["customfield_1", "customfield_2", "customfield_3"],
            )
        assert res["status"] == 200
        assert res["fields"]["customfield_1"] == "present"
        assert res["fields"]["customfield_2"] == "permission-error"
        assert res["fields"]["customfield_3"] == "null"


# ---------------------------------------------------------------------------
# run — orchestration, exit signalling, secret hygiene
# ---------------------------------------------------------------------------


class TestRun:
    def test_not_ok_when_jira_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        report = v.run(issue_key="SUPPORT-1", list_fields=False)
        assert report["ok"] is False
        assert report["reason"] == "jira_not_configured"

    def test_not_ok_when_no_fields_configured(self, primary_env: None) -> None:
        report = v.run(issue_key="SUPPORT-1", list_fields=False)
        assert report["ok"] is False
        assert report["reason"] == "no_refresh_fields_configured"

    def test_ok_when_field_present_on_domain_url(self, primary_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JIRA_REFRESH_FIELDS", "customfield_1:lunch")
        client = _mock_client(200, {"fields": {"customfield_1": {"value": "pizza"}}})
        with patch.object(v.httpx, "Client", return_value=client):
            report = v.run(issue_key="SUPPORT-1", list_fields=False)
        assert report["ok"] is True

    def test_not_ok_when_permission_error(self, primary_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JIRA_REFRESH_FIELDS", "customfield_1")
        client = _mock_client(200, {"fields": {"customfield_1": {"errorMessage": "no perm"}}})
        with patch.object(v.httpx, "Client", return_value=client):
            report = v.run(issue_key="SUPPORT-1", list_fields=False)
        assert report["ok"] is False

    def test_secret_never_in_rendered_output(
        self, primary_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("JIRA_REFRESH_FIELDS", "customfield_1")
        client = _mock_client(200, {"fields": {"customfield_1": {"value": "pizza"}}})
        with patch.object(v.httpx, "Client", return_value=client):
            v.run(issue_key="SUPPORT-1", list_fields=False)
        out = capsys.readouterr().out
        assert "secret-tok" not in out
