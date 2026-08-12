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
        with patch.object(jira_service.httpx, "Client", return_value=client):
            with pytest.raises(JiraFetchError):
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
        with patch.object(jira_service.httpx, "Client", return_value=client):
            with pytest.raises(JiraFetchError):
                svc.fetch_organization("325")

    def test_raises_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch, clear_org_env: None) -> None:
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)
        s = JiraService()
        s.domain = ""
        s.email = ""
        s.api_token = ""
        with patch.object(jira_service.httpx, "Client") as mock_cls:
            with pytest.raises(JiraFetchError):
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
        with patch.object(jira_service.httpx, "Client", return_value=client):
            with pytest.raises(JiraFetchError):
                svc.fetch_organization_ids()


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
        with patch.object(orgs, "get_jira_service", return_value=svc):
            with pytest.raises(JiraFetchError):
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
