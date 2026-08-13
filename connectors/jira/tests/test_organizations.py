"""
Tests for the organization-details path: JSM organization ids on tickets plus a
current-state `organizations` table carrying operator-configured detail fields.

Covers:
- organization_detail_fields(): parse JIRA_ORG_DETAIL_FIELDS (id or id:alias, validated)
- JiraService.resolve_cloud_id(): config value wins, else the site's tenant_info
- JiraService.fetch_organization(): CSM gateway URL, primary auth, status semantics
- JiraService.fetch_organization_ids(): paginated enumeration via servicedeskapi
- extract_organization_details(): id-primary with name fallback
- transform_issue(): organization_ids column (multi-org safe)
- transform_organization() / organizations_schema(): one column per configured detail
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from connectors.jira import service as jira_service
from connectors.jira.service import JiraFetchError, JiraService, organization_detail_fields
from connectors.jira.transform import (
    ORGANIZATIONS_SCHEMA,
    extract_organization_details,
    organizations_schema,
    transform_issue,
    transform_organization,
)

ORG_DETAIL_ENV = "JIRA_ORG_DETAIL_FIELDS"


@pytest.fixture()
def clear_org_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ORG_DETAIL_ENV, raising=False)


def _raw_issue(fields: dict) -> dict:
    return {"key": "SUPPORT-1", "id": "1", "fields": fields}


# ---------------------------------------------------------------------------
# organization_detail_fields() — parse JIRA_ORG_DETAIL_FIELDS
# ---------------------------------------------------------------------------


class TestOrganizationDetailFields:
    def test_empty_when_unset(self, clear_org_env: None) -> None:
        assert organization_detail_fields() == []

    def test_id_only_defaults_column_to_detail_id(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        # A bare numeric detail id is not a valid column name, so it gets the
        # generic prefix rather than producing `38` as a column.
        monkeypatch.setenv(ORG_DETAIL_ENV, "38")
        assert organization_detail_fields() == [("38", "detail_38")]

    def test_id_with_alias(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:crm_account_id")
        assert organization_detail_fields() == [("38", "crm_account_id")]

    def test_multiple_mixed_and_whitespace(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, " 38:crm_account_id , 41 ")
        assert organization_detail_fields() == [("38", "crm_account_id"), ("41", "detail_41")]

    def test_invalid_alias_falls_back(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:1bad")
        assert organization_detail_fields() == [("38", "detail_38")]

    def test_reserved_column_is_prefixed(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        # `name` is a built-in organizations column; a detail must never overwrite it.
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:name")
        assert organization_detail_fields() == [("38", "detail_name")]

    def test_duplicate_columns_first_wins(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:dup,41:dup")
        assert organization_detail_fields() == [("38", "dup")]

    def test_empty_entries_skipped(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:a,,41:b,")
        assert organization_detail_fields() == [("38", "a"), ("41", "b")]


# ---------------------------------------------------------------------------
# resolve_cloud_id() — config wins, else tenant_info
# ---------------------------------------------------------------------------


def _mock_client(status_code: int = 200, json_body=None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {} if json_body is None else json_body
    response.text = json.dumps({} if json_body is None else json_body)
    client = MagicMock()
    client.get.return_value = response
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


def _called_url(client: MagicMock) -> str:
    args, kwargs = client.get.call_args
    return args[0] if args else kwargs["url"]


@pytest.fixture()
def svc(monkeypatch: pytest.MonkeyPatch, clear_org_env: None) -> JiraService:
    monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "", raising=False)
    s = JiraService()
    s.domain = "mycompany.atlassian.net"
    s.email = "bot@mycompany.com"
    s.api_token = "tok-123"
    return s


class TestResolveCloudId:
    def test_config_value_wins_without_a_request(self, svc: JiraService, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        with patch.object(jira_service.httpx, "Client") as mock_cls:
            assert svc.resolve_cloud_id() == "cloud-xyz"
        mock_cls.assert_not_called()

    def test_falls_back_to_tenant_info(self, svc: JiraService) -> None:
        client = _mock_client(200, {"cloudId": "from-tenant-info"})
        with patch.object(jira_service.httpx, "Client", return_value=client):
            assert svc.resolve_cloud_id() == "from-tenant-info"
        assert _called_url(client) == "https://mycompany.atlassian.net/_edge/tenant_info"

    def test_tenant_info_result_is_cached(self, svc: JiraService) -> None:
        client = _mock_client(200, {"cloudId": "abc"})
        with patch.object(jira_service.httpx, "Client", return_value=client):
            svc.resolve_cloud_id()
            svc.resolve_cloud_id()
        assert client.get.call_count == 1

    def test_raises_when_tenant_info_fails(self, svc: JiraService) -> None:
        client = _mock_client(500, {})
        with patch.object(jira_service.httpx, "Client", return_value=client), pytest.raises(JiraFetchError):
            svc.resolve_cloud_id()


# ---------------------------------------------------------------------------
# fetch_organization() — CSM gateway URL, auth, status semantics
# ---------------------------------------------------------------------------


class TestFetchOrganization:
    def test_uses_csm_gateway_url_and_primary_auth(self, svc: JiraService, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        body = {"id": "325", "name": "Acme", "details": []}
        client = _mock_client(200, body)
        with patch.object(jira_service.httpx, "Client", return_value=client):
            assert svc.fetch_organization("325") == body
        assert _called_url(client) == ("https://api.atlassian.com/jsm/csm/cloudid/cloud-xyz/api/v1/organization/325")
        _, kwargs = client.get.call_args
        assert kwargs["auth"] == ("bot@mycompany.com", "tok-123")

    def test_returns_none_on_404(self, svc: JiraService, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        client = _mock_client(404, {})
        with patch.object(jira_service.httpx, "Client", return_value=client):
            assert svc.fetch_organization("999") is None

    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
    def test_raises_on_auth_ratelimit_and_server_errors(
        self, svc: JiraService, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        # Same contract as fetch_remote_links: a transient failure must NOT look
        # like "this org has no details", or the refresh would blank real values.
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        client = _mock_client(status, {})
        with patch.object(jira_service.httpx, "Client", return_value=client), pytest.raises(JiraFetchError):
            svc.fetch_organization("325")

    def test_raises_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch, clear_org_env: None) -> None:
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        s = JiraService()
        s.domain = ""
        s.email = ""
        s.api_token = ""
        with patch.object(jira_service.httpx, "Client") as mock_cls, pytest.raises(JiraFetchError):
            s.fetch_organization("325")
        mock_cls.assert_not_called()


class TestFetchOrganizationIds:
    def test_paginates_until_last_page(self, svc: JiraService) -> None:
        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "values": [{"id": "1"}, {"id": "2"}],
            "isLastPage": False,
            "size": 2,
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {"values": [{"id": "3"}], "isLastPage": True, "size": 1}
        client = MagicMock()
        client.get.side_effect = [page1, page2]
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)

        with patch.object(jira_service.httpx, "Client", return_value=client):
            assert svc.fetch_organization_ids() == ["1", "2", "3"]
        assert client.get.call_count == 2

    def test_raises_on_error_status(self, svc: JiraService) -> None:
        client = _mock_client(500, {})
        with patch.object(jira_service.httpx, "Client", return_value=client), pytest.raises(JiraFetchError):
            svc.fetch_organization_ids()

    def test_id_repeated_across_pages_is_returned_once(self, svc: JiraService) -> None:
        # Offset pagination over a mutating collection can serve the same
        # organization on two consecutive pages; a duplicate id becomes a
        # duplicate row in the lookup table and fans out every joined ticket
        # (Devin Review on #1274).
        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "values": [{"id": "1"}, {"id": "2"}],
            "isLastPage": False,
            "size": 2,
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {"values": [{"id": "2"}, {"id": "3"}], "isLastPage": True, "size": 2}
        client = MagicMock()
        client.get.side_effect = [page1, page2]
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)

        with patch.object(jira_service.httpx, "Client", return_value=client):
            assert svc.fetch_organization_ids() == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# extract_organization_details() — id-primary, name fallback
# ---------------------------------------------------------------------------


class TestExtractOrganizationDetails:
    def test_matches_on_detail_id(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:crm_account_id")
        org = {"id": "325", "details": [{"id": "38", "name": "CRM ID", "values": ["ACC-1"]}]}
        assert extract_organization_details(org) == {"crm_account_id": "ACC-1"}

    def test_id_wins_over_a_same_named_entry(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:crm_account_id")
        org = {
            "id": "325",
            "details": [
                {"name": "38", "values": ["wrong-by-name"]},
                {"id": "38", "name": "CRM ID", "values": ["right-by-id"]},
            ],
        }
        assert extract_organization_details(org) == {"crm_account_id": "right-by-id"}

    def test_falls_back_to_name_when_id_absent(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        # The batched CSM endpoints omit `id` entirely; a configured value that
        # names the label must still resolve.
        monkeypatch.setenv(ORG_DETAIL_ENV, "CRM ID:crm_account_id")
        org = {"id": "325", "details": [{"name": "CRM ID", "value": {"type": "TEXT", "text": ["ACC-2"]}}]}
        assert extract_organization_details(org) == {"crm_account_id": "ACC-2"}

    def test_absent_detail_is_none(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:crm_account_id")
        assert extract_organization_details({"id": "325", "details": []}) == {"crm_account_id": None}

    def test_empty_values_is_none(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:crm_account_id")
        org = {"id": "325", "details": [{"id": "38", "name": "CRM ID", "values": []}]}
        assert extract_organization_details(org) == {"crm_account_id": None}

    def test_nothing_configured_yields_no_columns(self, clear_org_env: None) -> None:
        org = {"id": "325", "details": [{"id": "38", "name": "CRM ID", "values": ["ACC-1"]}]}
        assert extract_organization_details(org) == {}


# ---------------------------------------------------------------------------
# transform_issue() — organization_ids column
# ---------------------------------------------------------------------------


class TestIssueOrganizationIds:
    def test_single_org(self, clear_org_env: None) -> None:
        rec = transform_issue(_raw_issue({"customfield_10002": [{"id": "325", "name": "Acme"}]}))
        assert json.loads(rec["organization_ids"]) == ["325"]

    def test_multi_org_keeps_every_id(self, clear_org_env: None) -> None:
        # 24 real tickets carry 2+ orgs; taking [0] would silently drop the rest.
        rec = transform_issue(
            _raw_issue(
                {
                    "customfield_10002": [
                        {"id": "325", "name": "Acme"},
                        {"id": "58", "name": "Globex"},
                    ]
                }
            )
        )
        assert json.loads(rec["organization_ids"]) == ["325", "58"]

    def test_absent_field_is_empty_array(self, clear_org_env: None) -> None:
        rec = transform_issue(_raw_issue({"summary": "x"}))
        assert json.loads(rec["organization_ids"]) == []

    def test_ids_are_strings(self, clear_org_env: None) -> None:
        # Jira has returned numeric ids in some payload shapes; the column is a
        # string so it joins against organizations.org_id without a cast.
        rec = transform_issue(_raw_issue({"customfield_10002": [{"id": 325, "name": "Acme"}]}))
        assert json.loads(rec["organization_ids"]) == ["325"]

    def test_entry_without_id_is_skipped(self, clear_org_env: None) -> None:
        rec = transform_issue(_raw_issue({"customfield_10002": [{"name": "No id here"}]}))
        assert json.loads(rec["organization_ids"]) == []

    def test_names_column_is_unchanged(self, clear_org_env: None) -> None:
        rec = transform_issue(_raw_issue({"customfield_10002": [{"id": "325", "name": "Acme"}]}))
        assert json.loads(rec["organizations"]) == ["Acme"]


# ---------------------------------------------------------------------------
# transform_organization() / organizations_schema()
# ---------------------------------------------------------------------------


class TestTransformOrganization:
    def test_base_columns(self, clear_org_env: None) -> None:
        rec = transform_organization({"id": "325", "name": "Acme", "details": []})
        assert rec["org_id"] == "325"
        assert rec["name"] == "Acme"

    def test_detail_column_populated(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:crm_account_id")
        rec = transform_organization(
            {"id": "325", "name": "Acme", "details": [{"id": "38", "name": "CRM ID", "values": ["ACC-1"]}]}
        )
        assert rec["crm_account_id"] == "ACC-1"

    def test_org_id_coerced_to_string(self, clear_org_env: None) -> None:
        rec = transform_organization({"id": 325, "name": "Acme", "details": []})
        assert rec["org_id"] == "325"

    def test_schema_has_base_columns_only_when_unconfigured(self, clear_org_env: None) -> None:
        assert organizations_schema() == dict(ORGANIZATIONS_SCHEMA)

    def test_schema_gains_a_column_per_configured_detail(
        self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:crm_account_id,41:region")
        schema = organizations_schema()
        assert schema["crm_account_id"] == "string"
        assert schema["region"] == "string"
        # base columns survive
        assert schema["org_id"] == "string"
        assert schema["name"] == "string"


# ---------------------------------------------------------------------------
# refresh_organizations() — failure semantics are the whole point here
# ---------------------------------------------------------------------------


def _fake_service(org_ids, fetch):
    svc = MagicMock()
    svc.is_configured.return_value = True
    svc.fetch_organization_ids.return_value = org_ids
    svc.fetch_organization.side_effect = fetch
    return svc


def _org(org_id: str, name: str, crm: str | None):
    details = [] if crm is None else [{"id": "38", "name": "CRM ID", "values": [crm]}]
    return {"id": org_id, "name": name, "details": details}


@pytest.fixture()
def org_env(monkeypatch: pytest.MonkeyPatch, clear_org_env: None) -> None:
    monkeypatch.setenv(ORG_DETAIL_ENV, "38:crm_account_id")


def _read_table(extract_dir: Path):
    import pandas as pd

    return pd.read_parquet(extract_dir / "data" / "organizations" / "data.parquet")


class TestRefreshOrganizations:
    def test_skips_when_jira_unconfigured(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        svc = MagicMock()
        svc.is_configured.return_value = False
        with patch.object(orgs, "get_jira_service", return_value=svc):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)
        assert stats["skipped_reason"] == "jira_not_configured"
        svc.fetch_organization_ids.assert_not_called()

    def test_writes_rows_with_detail_column(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1", "2"], [_org("1", "Acme", "ACC-1"), _org("2", "Globex", "ACC-2")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["written"] == 2
        df = _read_table(tmp_path)
        assert sorted(df["org_id"].tolist()) == ["1", "2"]
        assert dict(zip(df["org_id"], df["crm_account_id"])) == {"1": "ACC-1", "2": "ACC-2"}

    def test_enumeration_failure_aborts_before_any_write(self, tmp_path: Path, org_env: None) -> None:
        # A partial list is indistinguishable from organizations having been deleted,
        # so nothing may be written.
        from connectors.jira import organizations as orgs

        svc = MagicMock()
        svc.is_configured.return_value = True
        svc.fetch_organization_ids.side_effect = JiraFetchError("boom")
        with patch.object(orgs, "get_jira_service", return_value=svc), pytest.raises(JiraFetchError):
            orgs.refresh_organizations(extract_dir=tmp_path)
        assert not (tmp_path / "data" / "organizations" / "data.parquet").exists()

    def test_fetch_failure_preserves_the_previous_row(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        # First run establishes both rows.
        svc = _fake_service(["1", "2"], [_org("1", "Acme", "ACC-1"), _org("2", "Globex", "ACC-2")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)

        # Second run: org 2 rate-limits. Its value must survive, not blank out.
        svc2 = _fake_service(["1", "2"], [_org("1", "Acme", "ACC-1-NEW"), JiraFetchError("429")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["failed"] == 1
        assert stats["preserved"] == 1
        df = _read_table(tmp_path)
        mapping = dict(zip(df["org_id"], df["crm_account_id"]))
        assert mapping == {"1": "ACC-1-NEW", "2": "ACC-2"}

    def test_404_drops_the_row(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1", "2"], [_org("1", "Acme", "ACC-1"), _org("2", "Globex", "ACC-2")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)

        svc2 = _fake_service(["1", "2"], [_org("1", "Acme", "ACC-1"), None])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["removed"] == 1
        assert _read_table(tmp_path)["org_id"].tolist() == ["1"]

    def test_omission_drop_is_counted_as_removed(self, tmp_path: Path, org_env: None) -> None:
        # A row can leave the table without a 404: enumeration simply no longer
        # returns its id. Under the mass-removal threshold that drop is published,
        # so it must be reported — "0 removed" on a run that deleted a row is the
        # summary an operator acts on (Devin Review on #1274).
        from connectors.jira import organizations as orgs

        svc = _fake_service(
            ["1", "2", "3"],
            [_org("1", "Acme", "ACC-1"), _org("2", "Globex", "ACC-2"), _org("3", "Initech", "ACC-3")],
        )
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)

        svc2 = _fake_service(["1", "2"], [_org("1", "Acme", "ACC-1"), _org("2", "Globex", "ACC-2")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert "skipped_reason" not in stats
        assert stats["removed"] == 1
        assert sorted(_read_table(tmp_path)["org_id"].tolist()) == ["1", "2"]

    def test_all_fetches_failing_leaves_the_table_untouched(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1"], [JiraFetchError("500")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)
        assert stats["failed"] == 1
        assert stats["written"] == 0
        assert not (tmp_path / "data" / "organizations" / "data.parquet").exists()

    def test_dry_run_does_not_fetch_or_write(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1", "2"], [])
        with patch.object(orgs, "get_jira_service", return_value=svc):
            stats = orgs.refresh_organizations(extract_dir=tmp_path, dry_run=True)
        assert stats["organizations"] == 2
        assert stats["written"] == 0
        svc.fetch_organization.assert_not_called()
        assert not (tmp_path / "data" / "organizations" / "data.parquet").exists()

    def test_no_leftover_temp_file(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1"], [_org("1", "Acme", "ACC-1")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)
        table_dir = tmp_path / "data" / "organizations"
        assert [p.name for p in table_dir.glob("*")] == ["data.parquet"]


# ---------------------------------------------------------------------------
# extract_init — the flat (unpartitioned) table branch
# ---------------------------------------------------------------------------


class TestFlatTableRegistration:
    def test_organizations_is_registered_as_flat_not_hive(self) -> None:
        from connectors.jira.extract_init import JIRA_FLAT_TABLES, JIRA_TABLES

        assert "organizations" in JIRA_FLAT_TABLES
        assert "organizations" not in JIRA_TABLES

    def test_init_creates_meta_row_and_view(self, tmp_path: Path, org_env: None) -> None:
        import pandas as pd

        from connectors.jira.extract_init import init_extract
        from src.duckdb_conn import _open_duckdb

        table_dir = tmp_path / "data" / "organizations"
        table_dir.mkdir(parents=True)
        pd.DataFrame([{"org_id": "1", "name": "Acme", "crm_account_id": "ACC-1"}]).to_parquet(
            table_dir / "data.parquet"
        )

        init_extract(tmp_path)

        conn = _open_duckdb(str(tmp_path / "extract.duckdb"))
        try:
            rows = conn.execute("SELECT rows FROM _meta WHERE table_name = 'organizations'").fetchone()
            assert rows is not None and rows[0] == 1
            assert conn.execute("SELECT crm_account_id FROM organizations").fetchone()[0] == "ACC-1"
        finally:
            conn.close()

    def test_update_meta_inserts_when_row_is_absent(self, tmp_path: Path, org_env: None) -> None:
        # Upgrade path: an extract.duckdb built before this table existed has no
        # _meta row, so a bare UPDATE would leave the table out of the catalog.
        import pandas as pd

        from connectors.jira.extract_init import init_extract, update_meta
        from src.duckdb_conn import _open_duckdb

        init_extract(tmp_path)
        conn = _open_duckdb(str(tmp_path / "extract.duckdb"))
        try:
            conn.execute("DELETE FROM _meta WHERE table_name = 'organizations'")
        finally:
            conn.close()

        table_dir = tmp_path / "data" / "organizations"
        table_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"org_id": "1", "name": "Acme"}]).to_parquet(table_dir / "data.parquet")

        update_meta(tmp_path, "organizations")

        conn = _open_duckdb(str(tmp_path / "extract.duckdb"))
        try:
            row = conn.execute("SELECT rows FROM _meta WHERE table_name = 'organizations'").fetchone()
            assert row is not None and row[0] == 1
        finally:
            conn.close()


class TestReservedColumnsCoverEveryBuiltin:
    def test_no_builtin_column_can_be_shadowed_by_a_detail(
        self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every ORGANIZATIONS_SCHEMA column is protected, not just a hardcoded few.

        Guards the drift that a restated list invites: `_synced_at` was originally
        omitted, so `JIRA_ORG_DETAIL_FIELDS=38:_synced_at` passed validation and
        `transform_organization` overwrote the sync timestamp with a detail value.
        """
        for builtin in ORGANIZATIONS_SCHEMA:
            monkeypatch.setenv(ORG_DETAIL_ENV, f"38:{builtin}")
            assert organization_detail_fields() == [("38", f"detail_{builtin}")], (
                f"built-in column {builtin!r} is not reserved"
            )

    def test_detail_cannot_clobber_synced_at(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "38:_synced_at")
        rec = transform_organization(
            {"id": "325", "name": "Acme", "details": [{"id": "38", "name": "CRM ID", "values": ["CLOBBERED"]}]}
        )
        assert rec["_synced_at"] != "CLOBBERED"
        assert rec["detail__synced_at"] == "CLOBBERED"


class TestMalformedJsonStaysInsideTheErrorBoundary:
    """A 200 with a non-JSON body must raise JiraFetchError, not ValueError.

    The refresh sweep catches JiraFetchError per organization and preserves that
    organization's previous row. A bare ValueError escapes that handler and aborts
    the whole run before anything is written, so one bad response from an
    intermediary would discard every organization fetched so far.
    """

    @staticmethod
    def _bad_json_client() -> MagicMock:
        response = MagicMock()
        response.status_code = 200
        response.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
        client = MagicMock()
        client.get.return_value = response
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        return client

    def test_fetch_organization(self, svc: JiraService, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        with patch.object(jira_service.httpx, "Client", return_value=self._bad_json_client()):
            with pytest.raises(JiraFetchError):
                svc.fetch_organization("325")

    def test_fetch_organization_ids(self, svc: JiraService) -> None:
        with patch.object(jira_service.httpx, "Client", return_value=self._bad_json_client()):
            with pytest.raises(JiraFetchError):
                svc.fetch_organization_ids()

    def test_resolve_cloud_id(self, svc: JiraService) -> None:
        with patch.object(jira_service.httpx, "Client", return_value=self._bad_json_client()):
            with pytest.raises(JiraFetchError):
                svc.resolve_cloud_id()

    @staticmethod
    def _client_with_body(payload) -> MagicMock:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = payload
        client = MagicMock()
        client.get.return_value = response
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        return client

    def test_null_body_is_not_a_deletion(self, svc: JiraService, monkeypatch: pytest.MonkeyPatch) -> None:
        # A 200 whose body decodes to `null` must not be confused with the 404
        # None that means "the organization was deleted, drop its row".
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        with (
            patch.object(jira_service.httpx, "Client", return_value=self._client_with_body(None)),
            pytest.raises(JiraFetchError),
        ):
            svc.fetch_organization("325")

    @pytest.mark.parametrize("payload", [["not"], "an object", 7])
    def test_non_object_body_stays_inside_the_boundary(
        self, svc: JiraService, monkeypatch: pytest.MonkeyPatch, payload
    ) -> None:
        # A list/string/number body used to flow into `.get()` calls and raise
        # AttributeError outside the JiraFetchError boundary the sweep relies on.
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        client = self._client_with_body(payload)
        with patch.object(jira_service.httpx, "Client", return_value=client):
            with pytest.raises(JiraFetchError):
                svc.fetch_organization("325")
            with pytest.raises(JiraFetchError):
                svc.fetch_organization_ids()

    @pytest.mark.parametrize("payload", [["not"], "an object", 7])
    def test_non_object_tenant_info_stays_inside_the_boundary(self, svc: JiraService, payload) -> None:
        # Separate from the test above: resolve_cloud_id short-circuits when
        # JIRA_CLOUD_ID is configured, so it must be probed without it.
        with (
            patch.object(jira_service.httpx, "Client", return_value=self._client_with_body(payload)),
            pytest.raises(JiraFetchError),
        ):
            svc.resolve_cloud_id()

    def test_sweep_preserves_the_row_instead_of_aborting(self, tmp_path: Path, org_env: None) -> None:
        """End to end: the malformed response is absorbed per organization."""
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1", "2"], [_org("1", "Acme", "ACC-1"), _org("2", "Globex", "ACC-2")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)

        svc2 = _fake_service(
            ["1", "2"],
            [_org("1", "Acme", "ACC-1"), JiraFetchError("malformed JSON in a 200 response")],
        )
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["failed"] == 1 and stats["preserved"] == 1
        df = _read_table(tmp_path)
        assert dict(zip(df["org_id"], df["crm_account_id"])) == {"1": "ACC-1", "2": "ACC-2"}


class TestFlatTableSurvivesHiveMigration:
    """A dimension table's `data.parquet` must not be mistaken for a month partition.

    `migrate_flat_to_hive` globs every `*.parquet` directly under a table dir and
    treats the filename stem as a month key. Unguarded, `organizations/data.parquet`
    became `month=data/data.parquet` — which the flat view (a top-level `*.parquet`
    glob) then cannot see, so the table silently reported zero rows. `init_extract`
    skips flat tables, but the guard belongs in the migrator so a future caller
    cannot destroy the table by omission.
    """

    def test_data_parquet_is_not_migrated(self, tmp_path: Path) -> None:
        import pandas as pd

        from connectors.jira.incremental_transform import migrate_flat_to_hive

        table_dir = tmp_path / "organizations"
        table_dir.mkdir()
        pd.DataFrame([{"org_id": "1", "name": "Acme"}]).to_parquet(table_dir / "data.parquet")

        assert migrate_flat_to_hive(table_dir) == []
        assert (table_dir / "data.parquet").exists()
        assert not (table_dir / "month=data").exists()
        # Still visible to the flat view's top-level glob.
        assert [p.name for p in table_dir.glob("*.parquet")] == ["data.parquet"]

    def test_real_month_partitions_still_migrate(self, tmp_path: Path) -> None:
        import pandas as pd

        from connectors.jira.incremental_transform import migrate_flat_to_hive

        table_dir = tmp_path / "issues"
        table_dir.mkdir()
        pd.DataFrame([{"issue_key": "SUPPORT-1"}]).to_parquet(table_dir / "2026-01.parquet")

        assert migrate_flat_to_hive(table_dir) == ["2026-01"]
        assert (table_dir / "month=2026-01" / "data.parquet").exists()

    def test_init_extract_leaves_the_dimension_queryable(self, tmp_path: Path, org_env: None) -> None:
        """End to end: init_extract must not strand the table it just registered."""
        import pandas as pd

        from connectors.jira.extract_init import init_extract
        from src.duckdb_conn import _open_duckdb

        table_dir = tmp_path / "data" / "organizations"
        table_dir.mkdir(parents=True)
        pd.DataFrame([{"org_id": "1", "name": "Acme", "crm_account_id": "ACC-1"}]).to_parquet(
            table_dir / "data.parquet"
        )

        init_extract(tmp_path)

        conn = _open_duckdb(str(tmp_path / "extract.duckdb"))
        try:
            assert conn.execute("SELECT count(*) FROM organizations").fetchone()[0] == 1
        finally:
            conn.close()


class TestDevinReviewFindings:
    """Regressions for the four findings on PR #1274."""

    def test_reload_config_from_env_rehydrates_frozen_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_JiraConfig` snapshots os.environ in its class body, so a CLI that loads a
        .env in main() leaves the service holding empty credentials. The documented
        manual run then exits 0 reporting "Jira is not configured"."""
        from connectors.jira.service import reload_config_from_env

        monkeypatch.setattr(jira_service.Config, "JIRA_DOMAIN", "", raising=False)
        monkeypatch.setattr(jira_service.Config, "JIRA_EMAIL", "", raising=False)
        monkeypatch.setattr(jira_service.Config, "JIRA_API_TOKEN", "", raising=False)
        monkeypatch.setattr(jira_service, "_jira_service", jira_service.JiraService(), raising=False)
        assert jira_service.get_jira_service().is_configured() is False

        # What load_dotenv() does: populate os.environ, nothing else.
        monkeypatch.setenv("JIRA_DOMAIN", "mycompany.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "bot@mycompany.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "tok-123")
        assert jira_service.get_jira_service().is_configured() is False, "env alone must not be enough"

        reload_config_from_env()

        assert jira_service.Config.JIRA_DOMAIN == "mycompany.atlassian.net"
        svc = jira_service.get_jira_service()
        assert svc.is_configured() is True
        assert svc.domain == "mycompany.atlassian.net"

    def test_enumeration_uses_the_gateway_for_a_scoped_token(
        self, svc: JiraService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scoped token cannot authenticate against the site domain, so enumeration
        must follow fetch_refresh_fields onto the api.atlassian.com gateway."""
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"values": [{"id": "1"}], "isLastPage": True}
        client = MagicMock()
        client.get.return_value = response
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)

        with patch.object(jira_service.httpx, "Client", return_value=client):
            assert svc.fetch_organization_ids() == ["1"]

        assert _called_url(client) == ("https://api.atlassian.com/ex/jira/cloud-xyz/rest/servicedeskapi/organization")

    def test_enumeration_uses_the_site_domain_without_a_cloud_id(self, svc: JiraService) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"values": [{"id": "1"}], "isLastPage": True}
        client = MagicMock()
        client.get.return_value = response
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)

        with patch.object(jira_service.httpx, "Client", return_value=client):
            svc.fetch_organization_ids()

        assert _called_url(client) == "https://mycompany.atlassian.net/rest/servicedeskapi/organization"

    def test_scheduler_row_does_not_share_a_slot_with_another_daily_job(self) -> None:
        """Both LIGHT-lane, and the worker runs only _LIGHT_CONCURRENCY slots, so two
        long daily jobs on the same tick starve the rest of the lane."""
        from services.scheduler.__main__ import build_jobs

        daily: dict[str, list[str]] = {}
        for row in build_jobs():
            schedule = row[1]
            if isinstance(schedule, str) and schedule.startswith("daily "):
                daily.setdefault(schedule, []).append(row[0])

        ours = next(s for s, names in daily.items() if "jira-org-refresh" in names)
        assert daily[ours] == ["jira-org-refresh"], f"{ours} is shared with {daily[ours]}"

    def test_all_404_first_run_refuses_instead_of_publishing_empty(self, tmp_path: Path, org_env: None) -> None:
        # A token that can enumerate via the Service Desk API but not read via CSM
        # 404s every id. With no existing table the mass-removal guard is skipped,
        # so this used to publish an empty parquet and finalize the nightly job
        # `done` — and because the baseline then stays empty, the guard stayed
        # disabled and the empty publish repeated silently forever
        # (Devin Review on #1274).
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1", "2"], [None, None])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["skipped_reason"] == "all_fetches_failed"
        assert not (tmp_path / "data" / "organizations" / "data.parquet").exists()

    def test_all_404_refuses_even_with_force(self, tmp_path: Path, org_env: None) -> None:
        # --force exists to push an *intentional* truncate through the two refusals
        # that never self-clear. An all-404 sweep is enumerated-but-unreadable, not
        # a truncate anyone asked for, so force must not publish it either.
        from connectors.jira import organizations as orgs

        four = [_org(str(i), f"Org {i}", f"ACC-{i}") for i in range(1, 5)]
        svc = _fake_service(["1", "2", "3", "4"], four)
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)

        svc2 = _fake_service(["1", "2", "3", "4"], [None, None, None, None])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path, force=True)

        assert stats["skipped_reason"] == "all_fetches_failed"
        assert len(_read_table(tmp_path)) == 4, "the existing table must survive untouched"

    def test_mass_removal_guard_refuses_to_publish(self, tmp_path: Path, org_env: None) -> None:
        """A partial-visibility failure 404s some organizations without touching
        `failed`, which would silently delete the invisible half."""
        from connectors.jira import organizations as orgs

        four = [_org(str(i), f"Org {i}", f"ACC-{i}") for i in range(1, 5)]
        svc = _fake_service(["1", "2", "3", "4"], four)
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)
        assert len(_read_table(tmp_path)) == 4

        # Three of the four now 404 (readable via servicedeskapi, invisible via CSM).
        svc2 = _fake_service(["1", "2", "3", "4"], [_org("1", "Org 1", "ACC-1"), None, None, None])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["skipped_reason"] == "mass_removal_guard"
        assert len(_read_table(tmp_path)) == 4, "the existing table must survive untouched"

    def test_force_overrides_the_mass_removal_guard(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        four = [_org(str(i), f"Org {i}", f"ACC-{i}") for i in range(1, 5)]
        svc = _fake_service(["1", "2", "3", "4"], four)
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)

        svc2 = _fake_service(["1", "2", "3", "4"], [_org("1", "Org 1", "ACC-1"), None, None, None])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path, force=True)

        assert "skipped_reason" not in stats
        assert _read_table(tmp_path)["org_id"].tolist() == ["1"]

    def test_guard_allows_a_small_removal(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        four = [_org(str(i), f"Org {i}", f"ACC-{i}") for i in range(1, 5)]
        svc = _fake_service(["1", "2", "3", "4"], four)
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)

        # One of four gone: under the threshold, so it publishes.
        svc2 = _fake_service(
            ["1", "2", "3", "4"],
            [_org("1", "Org 1", "ACC-1"), _org("2", "Org 2", "ACC-2"), _org("3", "Org 3", "ACC-3"), None],
        )
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert "skipped_reason" not in stats
        assert sorted(_read_table(tmp_path)["org_id"].tolist()) == ["1", "2", "3"]

    def test_guard_does_not_fire_on_a_first_run(self, tmp_path: Path, org_env: None) -> None:
        """No existing table means there is nothing to protect."""
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1", "2"], [_org("1", "Org 1", "ACC-1"), None])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert "skipped_reason" not in stats
        assert _read_table(tmp_path)["org_id"].tolist() == ["1"]


class TestDevinReviewRoundTwo:
    """Regressions for the second review round on PR #1274."""

    def test_guard_counts_removals_not_net_size(self, tmp_path: Path, org_env: None) -> None:
        """Organizations added in the same sweep must not cancel out ones that vanished.

        Net arithmetic (`len(existing) - len(records)`) makes the guard unfireable on any
        growing site: 10 existing, 10 new, 6 old ones unreadable is a net of -4 while 60%
        of the table is being deleted.
        """
        from connectors.jira import organizations as orgs

        ten = [_org(str(i), f"Org {i}", f"ACC-{i}") for i in range(1, 11)]
        svc = _fake_service([str(i) for i in range(1, 11)], ten)
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)
        assert len(_read_table(tmp_path)) == 10

        # 4 of the original 10 survive, 6 are 404 — and 10 brand-new orgs appear.
        ids = [str(i) for i in range(1, 11)] + [f"new{i}" for i in range(10)]
        results = [_org(str(i), f"Org {i}", f"ACC-{i}") for i in range(1, 5)]
        results += [None] * 6
        results += [_org(f"new{i}", f"New {i}", f"NEW-{i}") for i in range(10)]
        svc2 = _fake_service(ids, results)
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["skipped_reason"] == "mass_removal_guard", "growth must not mask removals"
        assert len(_read_table(tmp_path)) == 10, "the existing table must survive untouched"

    def test_total_outage_does_not_republish(self, tmp_path: Path, org_env: None) -> None:
        """Every fetch failing on an existing table is a total failure, not a no-op run.

        It used to fall through on the preserved rows and rewrite a byte-identical
        parquet, refresh `_meta` and enqueue a rebuild — publishing nothing, on the one
        path where the API is known to be down.
        """
        from connectors.jira import organizations as orgs

        two = [_org("1", "Org 1", "ACC-1"), _org("2", "Org 2", "ACC-2")]
        svc = _fake_service(["1", "2"], two)
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)
        before = (tmp_path / "data" / "organizations" / "data.parquet").stat().st_mtime_ns

        svc2 = _fake_service(["1", "2"], [JiraFetchError("503"), JiraFetchError("503")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta") as meta,
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["skipped_reason"] == "all_fetches_failed"
        assert stats["failed"] == 2 and stats["written"] == 0
        meta.assert_not_called()
        after = (tmp_path / "data" / "organizations" / "data.parquet").stat().st_mtime_ns
        assert after == before, "the parquet must not be rewritten"

    def test_cli_and_worker_agree_on_what_counts_as_failure(self) -> None:
        """The two surfaces read the same set, so they cannot drift again."""
        from connectors.jira.organizations import FAILURE_REASONS

        assert FAILURE_REASONS == {
            "all_fetches_failed",
            "mass_removal_guard",
            "existing_unreadable",
            "enumeration_empty",
            "cloud_id_unresolved",
        }
        # An unconfigured instance skips this job; it does not fail it every night.
        assert "jira_not_configured" not in FAILURE_REASONS

    @pytest.mark.parametrize("reason", ["all_fetches_failed", "mass_removal_guard"])
    def test_worker_handler_raises_so_the_job_retries(self, reason: str) -> None:
        from app.worker import kinds

        with patch("connectors.jira.organizations.refresh_organizations", return_value={"skipped_reason": reason}):
            with pytest.raises(RuntimeError, match=reason):
                kinds._run_jira_org_refresh({})

    def test_worker_handler_stays_quiet_when_jira_is_unconfigured(self) -> None:
        from app.worker import kinds

        stats = {"skipped_reason": "jira_not_configured"}
        with patch("connectors.jira.organizations.refresh_organizations", return_value=stats):
            kinds._run_jira_org_refresh({})  # must not raise

    def test_cli_survives_missing_credentials(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """`load_config` validates creds and raises; the documented run must still report
        the clean 'not configured' skip rather than an unhandled traceback."""
        from connectors.jira import organizations as orgs

        for var in ("JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(sys, "argv", ["organizations", "--dry-run"])

        called = {}

        def _fake_refresh(**kwargs):
            called.update(kwargs)
            return {"skipped_reason": "jira_not_configured"}

        monkeypatch.setattr(orgs, "refresh_organizations", _fake_refresh)
        orgs.main()  # must not raise SystemExit or ValueError
        assert called == {"dry_run": True, "force": False}

    def test_cli_reports_enumeration_failure_without_a_traceback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enumeration failure is the one refusal that raises (so the worker job
        retries). On the documented manual command it must exit 1 with a logged
        message, not an unhandled JiraFetchError traceback — it is the first
        network call, so it is the most common transient of the set."""
        from connectors.jira import organizations as orgs

        for var in ("JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(sys, "argv", ["organizations"])

        def _boom(**kwargs):
            raise JiraFetchError("Organization enumeration failed: status 503")

        monkeypatch.setattr(orgs, "refresh_organizations", _boom)
        with pytest.raises(SystemExit) as exc:
            orgs.main()
        assert exc.value.code == 1


class TestUnreadableBaselineRefuses:
    """An unreadable existing table must not be treated as an empty one.

    `_read_existing` returning {} fed both safety nets: rows carried forward on a
    failed fetch, and the mass-removal guard (skipped when `existing` is falsy). A
    transient read error therefore disabled both at once, so any per-organization API
    failure in the same run silently deleted what it could not fetch.
    """

    def test_absent_table_reads_as_empty_not_unreadable(self, tmp_path: Path) -> None:
        from connectors.jira.organizations import _read_existing

        assert _read_existing(tmp_path / "organizations") == {}

    def test_corrupt_parquet_reads_as_unreadable(self, tmp_path: Path) -> None:
        from connectors.jira.organizations import _read_existing

        table_dir = tmp_path / "organizations"
        table_dir.mkdir()
        (table_dir / "data.parquet").write_bytes(b"not a parquet file at all")
        assert _read_existing(table_dir) is None

    def test_parquet_without_org_id_reads_as_unreadable(self, tmp_path: Path) -> None:
        import pandas as pd

        from connectors.jira.organizations import _read_existing

        table_dir = tmp_path / "organizations"
        table_dir.mkdir()
        pd.DataFrame([{"something_else": "x"}]).to_parquet(table_dir / "data.parquet")
        assert _read_existing(table_dir) is None

    def test_refresh_refuses_when_the_baseline_is_unreadable(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        table_dir = tmp_path / "data" / "organizations"
        table_dir.mkdir(parents=True)
        (table_dir / "data.parquet").write_bytes(b"corrupt")
        before = (table_dir / "data.parquet").read_bytes()

        svc = _fake_service(["1", "2"], [_org("1", "Org 1", "ACC-1"), JiraFetchError("429")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta") as meta,
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["skipped_reason"] == "existing_unreadable"
        assert stats["skipped_reason"] in orgs.FAILURE_REASONS, "must be a failure on both surfaces"
        meta.assert_not_called()
        svc.fetch_organization.assert_not_called()
        assert (table_dir / "data.parquet").read_bytes() == before

    def test_worker_raises_on_an_unreadable_baseline(self) -> None:
        from app.worker import kinds

        stats = {"skipped_reason": "existing_unreadable"}
        with patch("connectors.jira.organizations.refresh_organizations", return_value=stats):
            with pytest.raises(RuntimeError, match="existing_unreadable"):
                kinds._run_jira_org_refresh({})


class TestEmptyEnumerationSignals:
    """An empty enumeration against an existing table is not a healthy run.

    Leaving the rows alone is the right action, but reporting success meant a site
    that had organizations a moment ago and now enumerates none looked identical to a
    quiet nightly pass, on the only total-failure shape outside FAILURE_REASONS.
    """

    def test_empty_enumeration_with_existing_rows_is_a_failure(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        two = [_org("1", "Org 1", "ACC-1"), _org("2", "Org 2", "ACC-2")]
        svc = _fake_service(["1", "2"], two)
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)

        svc2 = _fake_service([], [])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta") as meta,
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["skipped_reason"] == "enumeration_empty"
        assert stats["skipped_reason"] in orgs.FAILURE_REASONS
        meta.assert_not_called()
        # The right action is still to leave the rows standing.
        assert len(_read_table(tmp_path)) == 2

    def test_empty_enumeration_without_an_existing_table_is_not_a_failure(self, tmp_path: Path, org_env: None) -> None:
        """A genuinely organization-free instance must not fail its nightly job."""
        from connectors.jira import organizations as orgs

        svc = _fake_service([], [])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert "skipped_reason" not in stats

    def test_unreadable_baseline_refuses_before_enumerating(self, tmp_path: Path, org_env: None) -> None:
        """The baseline read moved ahead of enumeration, so a refusal costs no API calls."""
        from connectors.jira import organizations as orgs

        table_dir = tmp_path / "data" / "organizations"
        table_dir.mkdir(parents=True)
        (table_dir / "data.parquet").write_bytes(b"corrupt")

        svc = _fake_service(["1"], [_org("1", "Org 1", "ACC-1")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["skipped_reason"] == "existing_unreadable"
        svc.fetch_organization_ids.assert_not_called()
        svc.fetch_organization.assert_not_called()


class TestConcurrentPublishSafety:
    """The nightly job and the documented manual run can overlap by design.

    `--force` exists for when the mass-removal guard is refusing on the scheduled
    path, so an operator running it during the nightly window is the intended use. A
    shared temp filename let either writer publish the other's half-written parquet.
    """

    def test_temp_name_is_per_process(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        seen: list[str] = []
        real_write = orgs.pq.write_table

        def _spy(table, where, **kw):
            seen.append(Path(where).name)
            return real_write(table, where, **kw)

        svc = _fake_service(["1"], [_org("1", "Acme", "ACC-1")])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
            patch.object(orgs.pq, "write_table", side_effect=_spy),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path)

        assert seen, "expected a parquet write"
        assert seen[0] == f"data.parquet.{os.getpid()}.tmp", seen
        # A second process would pick a different name, so neither can replace or
        # delete the other's in-flight temp.
        assert str(os.getpid()) in seen[0]

    def test_published_file_stays_group_readable(self, tmp_path: Path, org_env: None) -> None:
        """Guards the #203 class of regression: mkstemp would publish 0600 via os.replace.

        Run under a restrictive umask deliberately — pq.write_table creates the temp
        as 0666 & umask, so without an explicit chmod a 0077 umask (some container/
        systemd units) published 0600 and this test passed only because pytest
        inherits a permissive umask (Devin Review on #1274).
        """
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1"], [_org("1", "Acme", "ACC-1")])
        old_umask = os.umask(0o077)
        try:
            with (
                patch.object(orgs, "get_jira_service", return_value=svc),
                patch.object(orgs, "update_meta"),
                patch.object(orgs.time, "sleep"),
            ):
                orgs.refresh_organizations(extract_dir=tmp_path)
        finally:
            os.umask(old_umask)

        mode = (tmp_path / "data" / "organizations" / "data.parquet").stat().st_mode & 0o777
        assert mode == 0o660, f"published parquet must be 0660 like the sibling writers, got {oct(mode)}"

    def test_a_stray_temp_is_never_read_as_data(self, tmp_path: Path) -> None:
        """A crashed run leaves a temp behind; the view glob must not pick it up."""
        from connectors.jira.extract_init import _table_parquets

        table_dir = tmp_path / "organizations"
        table_dir.mkdir()
        (table_dir / "data.parquet").write_bytes(b"real")
        (table_dir / "data.parquet.99999.tmp").write_bytes(b"half-written")

        _, files = _table_parquets("organizations", table_dir)
        assert [f.name for f in files] == ["data.parquet"]


class TestCloudIdResolvedOnce:
    """An unresolvable cloud id must fail once, not once per organization.

    `fetch_organization` needs the cloud id per request but memoizes only successful
    lookups, and the sweep catches JiraFetchError per organization — so an unreachable
    tenant_info cost one 30s-timeout request per organization. At ~60 lookups per
    1800s lease, a few-hundred-organization site would be reclaimed mid-sweep and retried
    indefinitely.
    """

    def test_unresolvable_cloud_id_aborts_before_the_loop(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        svc = MagicMock()
        svc.is_configured.return_value = True
        svc.fetch_organization_ids.return_value = [str(i) for i in range(1, 51)]
        svc.resolve_cloud_id.side_effect = JiraFetchError("tenant_info lookup failed: connection")

        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta") as meta,
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["skipped_reason"] == "cloud_id_unresolved"
        assert stats["skipped_reason"] in orgs.FAILURE_REASONS
        # One resolution attempt for 50 organizations, and no per-org requests at all.
        assert svc.resolve_cloud_id.call_count == 1
        svc.fetch_organization.assert_not_called()
        meta.assert_not_called()

    def test_dry_run_does_not_require_cloud_id_resolution(self, tmp_path: Path, org_env: None) -> None:
        """--dry-run only enumerates, so it has no business needing CSM reachability."""
        from connectors.jira import organizations as orgs

        svc = MagicMock()
        svc.is_configured.return_value = True
        svc.fetch_organization_ids.return_value = ["1", "2"]
        svc.resolve_cloud_id.side_effect = JiraFetchError("should not be called")

        with patch.object(orgs, "get_jira_service", return_value=svc):
            stats = orgs.refresh_organizations(extract_dir=tmp_path, dry_run=True)

        assert "skipped_reason" not in stats
        svc.resolve_cloud_id.assert_not_called()

    def test_worker_raises_on_unresolvable_cloud_id(self) -> None:
        from app.worker import kinds

        stats = {"skipped_reason": "cloud_id_unresolved"}
        with patch("connectors.jira.organizations.refresh_organizations", return_value=stats):
            with pytest.raises(RuntimeError, match="cloud_id_unresolved"):
                kinds._run_jira_org_refresh({})


class TestForcedEmptyEnumeration:
    """`--force` must clear the empty-enumeration refusal, like the sibling guard.

    Neither refusal self-clears — the rows stay on disk, so the next run enumerates
    zero again and refuses again. Without a working escape hatch the only recovery on a
    site that really deleted every organization was removing the parquet by hand.
    """

    def _seed(self, tmp_path: Path, orgs_mod) -> None:
        two = [_org("1", "Org 1", "ACC-1"), _org("2", "Org 2", "ACC-2")]
        svc = _fake_service(["1", "2"], two)
        with (
            patch.object(orgs_mod, "get_jira_service", return_value=svc),
            patch.object(orgs_mod, "update_meta"),
            patch.object(orgs_mod.time, "sleep"),
        ):
            orgs_mod.refresh_organizations(extract_dir=tmp_path)

    def test_force_publishes_the_empty_state(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        self._seed(tmp_path, orgs)
        assert len(_read_table(tmp_path)) == 2

        svc = _fake_service([], [])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path, force=True)

        assert "skipped_reason" not in stats
        assert stats["removed"] == 2
        # The table is published empty, with its schema intact rather than deleted.
        df = _read_table(tmp_path)
        assert len(df) == 0
        assert "org_id" in df.columns and "crm_account_id" in df.columns

    def test_dry_run_previews_the_truncate_without_claiming_it(self, tmp_path: Path, org_env: None) -> None:
        # `--dry-run --force` on an empty enumeration must not report rows as
        # removed or log that it published: nothing was written, and a preview
        # that reports the destructive outcome reads as "the table is already
        # wiped" (Devin Review on #1274).
        from connectors.jira import organizations as orgs

        self._seed(tmp_path, orgs)

        svc = _fake_service([], [])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path, dry_run=True, force=True)

        assert stats["dry_run"] is True
        assert stats["removed"] == 0
        assert len(_read_table(tmp_path)) == 2

    def test_force_does_not_need_the_cloud_id(self, tmp_path: Path, org_env: None) -> None:
        """Nothing is fetched, so an unresolvable cloud id must not mask the truncate."""
        from connectors.jira import organizations as orgs

        self._seed(tmp_path, orgs)

        svc = MagicMock()
        svc.is_configured.return_value = True
        svc.fetch_organization_ids.return_value = []
        svc.resolve_cloud_id.side_effect = JiraFetchError("tenant_info unreachable")

        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path, force=True)

        assert "skipped_reason" not in stats
        svc.resolve_cloud_id.assert_not_called()
        assert len(_read_table(tmp_path)) == 0

    def test_recovery_is_permanent(self, tmp_path: Path, org_env: None) -> None:
        """After a forced clear, an ordinary run stops refusing — no existing rows left."""
        from connectors.jira import organizations as orgs

        self._seed(tmp_path, orgs)
        svc = _fake_service([], [])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            orgs.refresh_organizations(extract_dir=tmp_path, force=True)

        svc2 = _fake_service([], [])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc2),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert "skipped_reason" not in stats, "the refusal must not come back after a forced clear"

    def test_without_force_it_still_refuses(self, tmp_path: Path, org_env: None) -> None:
        from connectors.jira import organizations as orgs

        self._seed(tmp_path, orgs)
        svc = _fake_service([], [])
        with (
            patch.object(orgs, "get_jira_service", return_value=svc),
            patch.object(orgs, "update_meta"),
            patch.object(orgs.time, "sleep"),
        ):
            stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["skipped_reason"] == "enumeration_empty"
        assert len(_read_table(tmp_path)) == 2


class _StubJobs:
    """Records the idempotency keys enqueued.

    Only the FIRST enqueue reports the caller's status; the follow-up carries a
    distinct key and so never dedups onto anything.
    """

    def __init__(self, status: str = "queued") -> None:
        self.status = status
        self.keys: list[str | None] = []

    def enqueue(self, kind: str, payload: dict, idempotency_key: str | None = None) -> dict:
        self.keys.append(idempotency_key)
        return {"status": self.status if len(self.keys) == 1 else "queued"}


class TestMetaWriteIsSerialisedAgainstRebuilds:
    """The `_meta` write must sit inside `rebuild_mutex()` — and only that write.

    `update_meta` opens `extract.duckdb` for writing while a rebuild elsewhere holds the
    same file ATTACHed, and DuckDB is single-writer. A lost ATTACH is only logged, and
    the rebuild then swaps in a freshly built analytics database with no Jira views at
    all — so every Jira table disappears until a later rebuild wins. That is the live
    incident `app.worker.kinds._run_jira_refresh` documents, and this module is a second
    writer of the same file. Nothing pinned its placement here, so the lock could be
    moved or dropped without a single test going red.

    The other half is just as load-bearing: the mutex must NOT be held across the fetch
    sweep, which is one HTTP request per organization and runs for minutes on a large
    site. Holding it there would block every rebuild in the process for that whole time.
    """

    @staticmethod
    def _drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        import contextlib

        from connectors.jira import organizations as orgs

        events: list[str] = []

        @contextlib.contextmanager
        def _tracking_mutex():
            events.append("mutex:enter")
            try:
                yield
            finally:
                events.append("mutex:exit")

        def _fetch(org_id: str, client=None) -> dict:
            events.append(f"fetch:{org_id}")
            return _org(org_id, f"Org {org_id}", f"ACC-{org_id}")

        svc = MagicMock()
        svc.is_configured.return_value = True
        svc.fetch_organization_ids.return_value = ["1", "2"]
        svc.fetch_organization.side_effect = _fetch

        monkeypatch.setattr("src.orchestrator.rebuild_mutex", _tracking_mutex)
        monkeypatch.setattr(orgs, "update_meta", lambda _d, table: events.append(f"update_meta:{table}"))
        monkeypatch.setattr(orgs, "get_jira_service", lambda: svc)
        monkeypatch.setattr(orgs.time, "sleep", lambda _s: None)
        monkeypatch.setattr("src.repositories.jobs_repo", _StubJobs)

        stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["written"] == 2, f"the run under test must have published something: {stats}"
        return events

    def test_update_meta_runs_inside_the_mutex(
        self, tmp_path: Path, org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = self._drive(tmp_path, monkeypatch)

        meta = [i for i, e in enumerate(events) if e.startswith("update_meta:")]
        assert meta, "guard would assert nothing if the _meta pass never ran"
        assert events.count("mutex:enter") == 1, f"one critical section, not {events.count('mutex:enter')}: {events}"
        enter, left = events.index("mutex:enter"), events.index("mutex:exit")
        assert enter < min(meta) and max(meta) < left, f"the extract.duckdb write escaped the mutex: {events}"

    def test_the_fetch_sweep_stays_outside_the_mutex(
        self, tmp_path: Path, org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = self._drive(tmp_path, monkeypatch)

        fetches = [i for i, e in enumerate(events) if e.startswith("fetch:")]
        assert fetches, "guard would assert nothing if nothing was fetched"
        assert max(fetches) < events.index("mutex:enter"), (
            f"the mutex is held across the per-organization sweep, blocking every rebuild: {events}"
        )


class TestTheRefreshAnnouncesItsWrite:
    """A published parquet owes a rebuild, and that enqueue is not housekeeping.

    `_attach_and_create_views` skips any `_meta` row whose inner object did not exist
    when it ran, so after the first refresh the master analytics database carries no
    `jira_organizations` view until a rebuild runs *after* the write. Nothing else
    recovers it — the next scheduled refresh only enqueues if it publishes again, so the
    table would stay invisible for a day, or until an unrelated rebuild happened to fire.

    The sibling writers (the webhook path in `connectors/jira/service.py`, the SLA
    poller, the consistency checker) carry the identical contract and are guarded in
    `tests/test_jira_meta_refresh_cadence.py`; this path had no guard at all.
    """

    @staticmethod
    def _publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jobs: _StubJobs) -> dict:
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1"], [_org("1", "Acme", "ACC-1")])
        monkeypatch.setattr(orgs, "get_jira_service", lambda: svc)
        monkeypatch.setattr(orgs, "update_meta", lambda _d, _t: None)
        monkeypatch.setattr(orgs.time, "sleep", lambda _s: None)
        monkeypatch.setattr("src.repositories.jobs_repo", lambda: jobs)
        return orgs.refresh_organizations(extract_dir=tmp_path)

    def test_a_published_refresh_enqueues_one_coalesced_rebuild(
        self, tmp_path: Path, org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jobs = _StubJobs()

        stats = self._publish(tmp_path, monkeypatch, jobs)

        assert stats["written"] == 1
        assert jobs.keys == ["jira-refresh"], "one coalesced rebuild per publish"

    def test_dedup_onto_a_running_rebuild_gets_a_follow_up(
        self, tmp_path: Path, org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A RUNNING rebuild may have read the extract before this write landed.

        The dedup matches `status IN ('queued', 'running')`, so collapsing onto a running
        job is not enough — the same invariant the webhook path states.
        """
        jobs = _StubJobs(status="running")

        self._publish(tmp_path, monkeypatch, jobs)

        assert jobs.keys == ["jira-refresh", "jira-refresh-followup"]

    def test_a_refusal_to_publish_enqueues_nothing(
        self, tmp_path: Path, org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusals leave the table exactly as it was; there is nothing to publish."""
        from connectors.jira import organizations as orgs

        self._publish(tmp_path, monkeypatch, _StubJobs())

        jobs = _StubJobs()
        svc = _fake_service([], [])
        monkeypatch.setattr(orgs, "get_jira_service", lambda: svc)
        monkeypatch.setattr("src.repositories.jobs_repo", lambda: jobs)

        stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["skipped_reason"] == "enumeration_empty"
        assert jobs.keys == [], "a run that published nothing must not ask for a rebuild"

    def test_an_unreachable_job_queue_does_not_fail_the_refresh(
        self, tmp_path: Path, org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The module also runs as a standalone script, and the parquet is already durable."""
        from connectors.jira import organizations as orgs

        svc = _fake_service(["1"], [_org("1", "Acme", "ACC-1")])
        monkeypatch.setattr(orgs, "get_jira_service", lambda: svc)
        monkeypatch.setattr(orgs, "update_meta", lambda _d, _t: None)
        monkeypatch.setattr(orgs.time, "sleep", lambda _s: None)

        def _boom() -> None:
            raise RuntimeError("no job queue in this process")

        monkeypatch.setattr("src.repositories.jobs_repo", _boom)

        stats = orgs.refresh_organizations(extract_dir=tmp_path)

        assert stats["written"] == 1
        assert "skipped_reason" not in stats
        assert len(_read_table(tmp_path)) == 1
