"""
Tests for issue #1273: "tickets cannot be joined to organization data — only
drifting names are captured".

Two halves:

1. ``issues.organization_ids`` — ``transform_issue`` captures the (stable) Jira
   organization ids alongside the existing (rename-fragile) names, reading the
   SAME ``customfield_10002`` entries ``extract_option_list`` already reads for
   ``organizations``.
2. A current-state ``organizations`` dimension table — enumerated via the classic
   JSM ``GET /rest/servicedeskapi/organization`` (paginated; there is no CSM list
   endpoint — ``GET /organization`` answers 405, ``POST`` there creates one), with
   operator-configured detail fields resolved via the CSM
   ``GET /organization/{id}`` endpoint (id-first, name fallback — the detail id is
   an observed, undocumented property of that response). Refreshed on a
   low-frequency cadence piggybacked onto the existing ``jira-refresh`` job (see
   ``tests/test_jira_meta_refresh_cadence.py``), not a new scheduler.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from connectors.jira import organizations as jira_orgs
from connectors.jira import service as jira_service
from connectors.jira.service import JiraFetchError
from connectors.jira.transform import extract_option_id_list, transform_issue

ORG_DETAIL_ENV = "JIRA_ORG_DETAIL_FIELDS"


@pytest.fixture()
def clear_org_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ORG_DETAIL_ENV, raising=False)
    monkeypatch.delenv("JIRA_ORG_REFRESH_INTERVAL_DAYS", raising=False)


# ---------------------------------------------------------------------------
# 1a. transform_issue() captures organization_ids from customfield_10002
# ---------------------------------------------------------------------------


class TestTransformIssueOrganizationIds:
    @staticmethod
    def _raw(customfield_10002) -> dict:
        return {"key": "SUPPORT-1", "id": "1", "fields": {"customfield_10002": customfield_10002}}

    def test_single_organization(self) -> None:
        rec = transform_issue(self._raw([{"id": "42", "uuid": "u-42", "name": "Charlie Cakes"}]))
        assert json.loads(rec["organization_ids"]) == ["42"]
        # the existing (rename-fragile) name column is untouched
        assert json.loads(rec["organizations"]) == ["Charlie Cakes"]

    def test_multi_valued_captures_every_id(self) -> None:
        """24 tickets on the production instance measured in #1273 carry 2+ orgs
        (up to 4) — taking the first would silently drop the rest."""
        rec = transform_issue(
            self._raw(
                [
                    {"id": "1", "uuid": "u-1", "name": "Acme"},
                    {"id": "2", "uuid": "u-2", "name": "Beta"},
                    {"id": "3", "uuid": "u-3", "name": "Gamma"},
                ]
            )
        )
        assert json.loads(rec["organization_ids"]) == ["1", "2", "3"]
        assert json.loads(rec["organizations"]) == ["Acme", "Beta", "Gamma"]

    def test_missing_field_is_empty_array_not_null(self) -> None:
        rec = transform_issue(self._raw(None))
        assert json.loads(rec["organization_ids"]) == []

    def test_field_absent_entirely_is_empty_array(self) -> None:
        rec = transform_issue({"key": "SUPPORT-1", "id": "1", "fields": {}})
        assert json.loads(rec["organization_ids"]) == []

    def test_entry_missing_id_is_skipped_not_crashing(self) -> None:
        rec = transform_issue(self._raw([{"uuid": "u-1", "name": "No Id Org"}, {"id": "9", "name": "Has Id"}]))
        assert json.loads(rec["organization_ids"]) == ["9"]


class TestExtractOptionIdList:
    def test_empty_on_none(self) -> None:
        assert extract_option_id_list(None) == []

    def test_empty_on_non_list(self) -> None:
        assert extract_option_id_list({"id": "1"}) == []

    def test_ids_as_strings(self) -> None:
        assert extract_option_id_list([{"id": 42, "name": "x"}]) == ["42"]


# ---------------------------------------------------------------------------
# 1b. org_detail_fields() — parse JIRA_ORG_DETAIL_FIELDS (same shape as
#     JIRA_REFRESH_FIELDS)
# ---------------------------------------------------------------------------


class TestOrgDetailFields:
    def test_empty_when_unset(self, clear_org_env: None) -> None:
        assert jira_orgs.org_detail_fields() == []

    def test_id_only_defaults_column_to_id(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "321")
        assert jira_orgs.org_detail_fields() == [("321", "321")]

    def test_id_with_alias(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:crm_account_id")
        assert jira_orgs.org_detail_fields() == [("321", "crm_account_id")]

    def test_multiple_mixed(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:crm_account_id, 654:region")
        assert jira_orgs.org_detail_fields() == [("321", "crm_account_id"), ("654", "region")]


class TestResolvedOrgDetailColumns:
    def test_collision_with_builtin_is_prefixed(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:name")
        cols = jira_orgs.resolved_org_detail_columns()
        assert cols == [("321", f"{jira_orgs.DETAIL_COLLISION_PREFIX}name")]

    def test_duplicate_alias_first_wins(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:tier,654:tier")
        assert jira_orgs.resolved_org_detail_columns() == [("321", "tier")]

    def test_organizations_schema_includes_detail_columns(
        self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:crm_account_id")
        schema = jira_orgs.organizations_schema()
        assert schema["org_id"] == "string"
        assert schema["name"] == "string"
        assert schema["crm_account_id"] == "string"


# ---------------------------------------------------------------------------
# 1c. resolve_detail_values() — id-first with a name fallback
# ---------------------------------------------------------------------------


class TestResolveDetailValues:
    def test_matches_by_id_first(self) -> None:
        details = [{"id": "321", "name": "Region", "values": ["EU"]}]
        out = jira_orgs.resolve_detail_values(details, [("321", "region")])
        assert out == {"region": "EU"}

    def test_falls_back_to_name_when_id_not_found(self) -> None:
        """The detail id is an observed, undocumented property — a configured
        token that never matches an id should still resolve via the name."""
        details = [{"id": "999", "name": "Region", "values": ["EU"]}]
        out = jira_orgs.resolve_detail_values(details, [("Region", "region")])
        assert out == {"region": "EU"}

    def test_id_match_wins_over_name_match(self) -> None:
        details = [
            {"id": "321", "name": "Other", "values": ["by-id"]},
            {"id": "999", "name": "321", "values": ["by-name"]},
        ]
        out = jira_orgs.resolve_detail_values(details, [("321", "col")])
        assert out == {"col": "by-id"}

    def test_no_match_is_none_not_a_crash(self) -> None:
        details = [{"id": "1", "name": "Other", "values": ["x"]}]
        out = jira_orgs.resolve_detail_values(details, [("321", "region")])
        assert out == {"region": None}

    def test_empty_values_list_is_none(self) -> None:
        details = [{"id": "321", "name": "Region", "values": []}]
        out = jira_orgs.resolve_detail_values(details, [("321", "region")])
        assert out == {"region": None}

    def test_multiple_configured_fields(self) -> None:
        details = [
            {"id": "321", "name": "Region", "values": ["EU"]},
            {"id": "654", "name": "Tier", "values": ["gold"]},
        ]
        out = jira_orgs.resolve_detail_values(details, [("321", "region"), ("654", "tier")])
        assert out == {"region": "EU", "tier": "gold"}

    def test_empty_details_list(self) -> None:
        out = jira_orgs.resolve_detail_values([], [("321", "region")])
        assert out == {"region": None}


# ---------------------------------------------------------------------------
# 1c. fetch_organizations() — paginated enumeration via the classic JSM API
# ---------------------------------------------------------------------------


def _resp(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    return r


def _mock_client(*responses: MagicMock) -> MagicMock:
    client = MagicMock()
    client.get.side_effect = list(responses)
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


class TestFetchOrganizations:
    def test_single_page(self) -> None:
        client = _mock_client(
            _resp(
                200,
                {
                    "isLastPage": True,
                    "values": [{"id": "1", "name": "Acme"}, {"id": "2", "name": "Beta"}],
                },
            )
        )
        with patch.object(jira_orgs.httpx, "Client", return_value=client):
            orgs = jira_orgs.fetch_organizations("mycompany.atlassian.net", ("e@x.com", "tok"))
        assert orgs == [{"id": "1", "name": "Acme"}, {"id": "2", "name": "Beta"}]

    def test_paginates_until_last_page(self) -> None:
        client = _mock_client(
            _resp(200, {"isLastPage": False, "values": [{"id": "1", "name": "Acme"}]}),
            _resp(200, {"isLastPage": False, "values": [{"id": "2", "name": "Beta"}]}),
            _resp(200, {"isLastPage": True, "values": [{"id": "3", "name": "Gamma"}]}),
        )
        with patch.object(jira_orgs.httpx, "Client", return_value=client):
            orgs = jira_orgs.fetch_organizations("mycompany.atlassian.net", ("e@x.com", "tok"), page_size=1)
        assert [o["id"] for o in orgs] == ["1", "2", "3"]
        assert client.get.call_count == 3
        # second call must ask for the next page (start advanced past page 1)
        _, kwargs = client.get.call_args_list[1]
        assert kwargs["params"]["start"] == 1
        assert kwargs["params"]["limit"] == 1

    def test_url_is_the_classic_servicedeskapi_not_csm(self) -> None:
        client = _mock_client(_resp(200, {"isLastPage": True, "values": []}))
        with patch.object(jira_orgs.httpx, "Client", return_value=client):
            jira_orgs.fetch_organizations("mycompany.atlassian.net", ("e@x.com", "tok"))
        args, kwargs = client.get.call_args
        url = args[0] if args else kwargs["url"]
        assert url == "https://mycompany.atlassian.net/rest/servicedeskapi/organization"

    def test_non_200_raises_jira_fetch_error(self) -> None:
        client = _mock_client(_resp(500, {}))
        with patch.object(jira_orgs.httpx, "Client", return_value=client):
            with pytest.raises(JiraFetchError):
                jira_orgs.fetch_organizations("mycompany.atlassian.net", ("e@x.com", "tok"))

    def test_request_error_raises_jira_fetch_error(self) -> None:
        client = MagicMock()
        client.get.side_effect = jira_orgs.httpx.RequestError("boom")
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        with patch.object(jira_orgs.httpx, "Client", return_value=client):
            with pytest.raises(JiraFetchError):
                jira_orgs.fetch_organizations("mycompany.atlassian.net", ("e@x.com", "tok"))

    def test_stalled_pagination_raises_rather_than_looping_forever(self) -> None:
        """An empty page with isLastPage=False is a malformed/inconsistent
        response — abort loudly instead of looping on an unchanged `start`."""
        client = _mock_client(_resp(200, {"isLastPage": False, "values": []}))
        with patch.object(jira_orgs.httpx, "Client", return_value=client):
            with pytest.raises(JiraFetchError):
                jira_orgs.fetch_organizations("mycompany.atlassian.net", ("e@x.com", "tok"))

    def test_entries_missing_id_are_skipped(self) -> None:
        client = _mock_client(
            _resp(200, {"isLastPage": True, "values": [{"name": "No Id"}, {"id": "9", "name": "Has Id"}]})
        )
        with patch.object(jira_orgs.httpx, "Client", return_value=client):
            orgs = jira_orgs.fetch_organizations("mycompany.atlassian.net", ("e@x.com", "tok"))
        assert orgs == [{"id": "9", "name": "Has Id"}]


# ---------------------------------------------------------------------------
# 1c. fetch_organization_detail() — CSM per-organization endpoint, fail-soft
# ---------------------------------------------------------------------------


class TestFetchOrganizationDetail:
    def test_success_returns_parsed_body(self) -> None:
        body = {"id": "1", "name": "Acme", "details": [{"id": "321", "name": "Region", "values": ["EU"]}]}
        client = _mock_client(_resp(200, body))
        with patch.object(jira_orgs.httpx, "Client", return_value=client):
            result = jira_orgs.fetch_organization_detail("cloud-xyz", "1", ("e@x.com", "tok"))
        assert result == body
        args, kwargs = client.get.call_args
        url = args[0] if args else kwargs["url"]
        assert url == "https://api.atlassian.com/jsm/csm/cloudid/cloud-xyz/api/v1/organization/1"

    @pytest.mark.parametrize("status_code", [401, 403, 404, 429, 500, 503])
    def test_failure_status_returns_none_never_raises(self, status_code: int) -> None:
        client = _mock_client(_resp(status_code, {}))
        with patch.object(jira_orgs.httpx, "Client", return_value=client):
            result = jira_orgs.fetch_organization_detail("cloud-xyz", "1", ("e@x.com", "tok"))
        assert result is None

    def test_request_error_returns_none_never_raises(self) -> None:
        client = MagicMock()
        client.get.side_effect = jira_orgs.httpx.RequestError("boom")
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        with patch.object(jira_orgs.httpx, "Client", return_value=client):
            result = jira_orgs.fetch_organization_detail("cloud-xyz", "1", ("e@x.com", "tok"))
        assert result is None


# ---------------------------------------------------------------------------
# build_organization_rows() — fail-soft merge with the previous snapshot
# ---------------------------------------------------------------------------


class TestBuildOrganizationRows:
    def test_uses_fresh_detail_when_available(self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:region")
        orgs = [{"id": "1", "name": "Acme"}]
        detail_by_org = {
            "1": {"id": "1", "name": "Acme", "details": [{"id": "321", "name": "Region", "values": ["EU"]}]}
        }
        rows = jira_orgs.build_organization_rows(orgs, detail_by_org, previous_by_org={})
        assert rows == [{"org_id": "1", "name": "Acme", "region": "EU"}]

    def test_failed_detail_fetch_keeps_previous_value(
        self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient 429 on one organization must not blank a value a
        previous run resolved correctly (issue #1273)."""
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:region")
        orgs = [{"id": "1", "name": "Acme"}]
        detail_by_org = {"1": None}
        previous_by_org = {"1": {"org_id": "1", "name": "Acme", "region": "EU"}}
        rows = jira_orgs.build_organization_rows(orgs, detail_by_org, previous_by_org)
        assert rows == [{"org_id": "1", "name": "Acme", "region": "EU"}]

    def test_failed_detail_fetch_with_no_previous_is_none_not_dropped(
        self, clear_org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail soft per organization: the row survives even with no detail
        value ever resolved — it is not dropped from the table."""
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:region")
        orgs = [{"id": "1", "name": "Acme"}]
        rows = jira_orgs.build_organization_rows(orgs, detail_by_org={"1": None}, previous_by_org={})
        assert rows == [{"org_id": "1", "name": "Acme", "region": None}]

    def test_no_configured_detail_fields_still_emits_org_id_and_name(self, clear_org_env: None) -> None:
        orgs = [{"id": "1", "name": "Acme"}]
        rows = jira_orgs.build_organization_rows(orgs, detail_by_org={}, previous_by_org={})
        assert rows == [{"org_id": "1", "name": "Acme"}]


# ---------------------------------------------------------------------------
# sync_organizations() — end-to-end write + fail-soft enumeration guard
# ---------------------------------------------------------------------------


@pytest.fixture()
def configured_org_service(monkeypatch: pytest.MonkeyPatch, clear_org_env: None) -> None:
    monkeypatch.setattr(jira_service.Config, "JIRA_DOMAIN", "mycompany.atlassian.net", raising=False)
    monkeypatch.setattr(jira_service.Config, "JIRA_EMAIL", "bot@mycompany.com", raising=False)
    monkeypatch.setattr(jira_service.Config, "JIRA_API_TOKEN", "tok-123", raising=False)
    monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "cloud-xyz", raising=False)


class TestSyncOrganizations:
    def test_writes_parquet_and_meta_row(
        self, tmp_path: Path, configured_org_service: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:region")
        monkeypatch.setattr(
            jira_orgs,
            "fetch_organizations",
            lambda domain, auth, **kw: [{"id": "1", "name": "Acme"}, {"id": "2", "name": "Beta"}],
        )
        monkeypatch.setattr(
            jira_orgs,
            "fetch_organization_detail",
            lambda cloud_id, org_id, auth: {
                "id": org_id,
                "name": "x",
                "details": [{"id": "321", "name": "Region", "values": ["EU"]}],
            },
        )

        stats = jira_orgs.sync_organizations(tmp_path)

        assert stats["organizations"] == 2
        pq_path = tmp_path / "data" / "organizations.parquet"
        assert pq_path.exists()
        df = pd.read_parquet(pq_path).sort_values("org_id").reset_index(drop=True)
        assert list(df["org_id"]) == ["1", "2"]
        assert list(df["region"]) == ["EU", "EU"]

        from src.duckdb_conn import _open_duckdb

        conn = _open_duckdb(str(tmp_path / "extract.duckdb"), read_only=True)
        try:
            meta_row = conn.execute("SELECT rows, query_mode FROM _meta WHERE table_name = 'organizations'").fetchone()
            assert meta_row == (2, "local")
            view_rows = conn.execute("SELECT count(*) FROM organizations").fetchone()[0]
            assert view_rows == 2
        finally:
            conn.close()

    def test_enumeration_failure_leaves_existing_table_untouched(
        self, tmp_path: Path, configured_org_service: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(jira_orgs, "fetch_organizations", lambda domain, auth, **kw: [{"id": "1", "name": "Acme"}])
        jira_orgs.sync_organizations(tmp_path)
        pq_path = tmp_path / "data" / "organizations.parquet"
        before = pq_path.read_bytes()

        def _boom(domain, auth, **kw):
            raise JiraFetchError("Jira is down")

        monkeypatch.setattr(jira_orgs, "fetch_organizations", _boom)
        stats = jira_orgs.sync_organizations(tmp_path)

        assert stats.get("skipped")
        assert pq_path.read_bytes() == before

    def test_a_failed_detail_fetch_preserves_the_previous_run_value(
        self, tmp_path: Path, configured_org_service: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:region")
        monkeypatch.setattr(jira_orgs, "fetch_organizations", lambda domain, auth, **kw: [{"id": "1", "name": "Acme"}])
        monkeypatch.setattr(
            jira_orgs,
            "fetch_organization_detail",
            lambda cloud_id, org_id, auth: {
                "id": org_id,
                "name": "x",
                "details": [{"id": "321", "name": "Region", "values": ["EU"]}],
            },
        )
        jira_orgs.sync_organizations(tmp_path)

        # second run: this organization's detail fetch fails (429) — the
        # previously-resolved value must survive, not go blank.
        monkeypatch.setattr(jira_orgs, "fetch_organization_detail", lambda cloud_id, org_id, auth: None)
        jira_orgs.sync_organizations(tmp_path)

        df = pd.read_parquet(tmp_path / "data" / "organizations.parquet")
        assert df.iloc[0]["region"] == "EU"

    def test_not_configured_skips_without_network_call(
        self, tmp_path: Path, clear_org_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Explicit blanks rather than relying on ambient env: `Config` attributes
        # are plain class attributes, and other Jira test modules mutate them
        # directly (not via monkeypatch), so they can leak across the session
        # depending on test order.
        monkeypatch.setattr(jira_service.Config, "JIRA_DOMAIN", "", raising=False)
        monkeypatch.setattr(jira_service.Config, "JIRA_EMAIL", "", raising=False)
        monkeypatch.setattr(jira_service.Config, "JIRA_API_TOKEN", "", raising=False)
        with patch.object(jira_orgs, "fetch_organizations") as fake_fetch:
            stats = jira_orgs.sync_organizations(tmp_path)
        fake_fetch.assert_not_called()
        assert stats.get("skipped")

    def test_detail_fields_configured_without_cloud_id_skips_detail_fetch_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear_org_env: None
    ) -> None:
        monkeypatch.setattr(jira_service.Config, "JIRA_DOMAIN", "mycompany.atlassian.net", raising=False)
        monkeypatch.setattr(jira_service.Config, "JIRA_EMAIL", "bot@mycompany.com", raising=False)
        monkeypatch.setattr(jira_service.Config, "JIRA_API_TOKEN", "tok-123", raising=False)
        monkeypatch.setattr(jira_service.Config, "JIRA_CLOUD_ID", "", raising=False)
        monkeypatch.setenv(ORG_DETAIL_ENV, "321:region")
        monkeypatch.setattr(jira_orgs, "fetch_organizations", lambda domain, auth, **kw: [{"id": "1", "name": "Acme"}])
        with patch.object(jira_orgs, "fetch_organization_detail") as fake_detail:
            stats = jira_orgs.sync_organizations(tmp_path)
        fake_detail.assert_not_called()
        assert stats["organizations"] == 1
        df = pd.read_parquet(tmp_path / "data" / "organizations.parquet")
        assert pd.isna(df.iloc[0]["region"])


# ---------------------------------------------------------------------------
# 1d. Cadence gating — piggybacks on the existing jira-refresh job with a
#     longer period, rather than a new scheduler.
# ---------------------------------------------------------------------------


class TestOrganizationsStale:
    def test_missing_extract_db_is_stale(self, tmp_path: Path) -> None:
        assert jira_orgs.organizations_stale(tmp_path) is True

    def test_missing_meta_row_is_stale(self, tmp_path: Path) -> None:
        from connectors.jira.extract_init import init_extract

        init_extract(tmp_path)  # creates _meta with the 6 event tables, no 'organizations' row
        assert jira_orgs.organizations_stale(tmp_path) is True

    def test_fresh_extracted_at_is_not_stale(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JIRA_ORG_REFRESH_INTERVAL_DAYS", raising=False)
        jira_orgs._write_organizations_table(tmp_path, [{"org_id": "1", "name": "Acme"}])
        now = datetime.now(timezone.utc)
        assert jira_orgs.organizations_stale(tmp_path, now=now) is False

    def test_old_extracted_at_is_stale(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JIRA_ORG_REFRESH_INTERVAL_DAYS", raising=False)
        jira_orgs._write_organizations_table(tmp_path, [{"org_id": "1", "name": "Acme"}])
        far_future = datetime.now(timezone.utc) + timedelta(days=jira_orgs.DEFAULT_ORG_REFRESH_INTERVAL_DAYS + 1)
        assert jira_orgs.organizations_stale(tmp_path, now=far_future) is True

    def test_custom_interval_is_honored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JIRA_ORG_REFRESH_INTERVAL_DAYS", "1")
        jira_orgs._write_organizations_table(tmp_path, [{"org_id": "1", "name": "Acme"}])
        two_days_later = datetime.now(timezone.utc) + timedelta(days=2)
        assert jira_orgs.organizations_stale(tmp_path, now=two_days_later) is True


class TestRefreshOrganizationsIfStale:
    def test_fresh_table_skips_sync_entirely(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jira_orgs, "organizations_stale", lambda _d, now=None: False)
        with patch.object(jira_orgs, "sync_organizations") as fake_sync:
            result = jira_orgs.refresh_organizations_if_stale(tmp_path)
        fake_sync.assert_not_called()
        assert result is None

    def test_stale_table_triggers_a_sync(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jira_orgs, "organizations_stale", lambda _d, now=None: True)
        with patch.object(jira_orgs, "sync_organizations", return_value={"organizations": 3}) as fake_sync:
            result = jira_orgs.refresh_organizations_if_stale(tmp_path)
        fake_sync.assert_called_once_with(Path(tmp_path))
        assert result == {"organizations": 3}
