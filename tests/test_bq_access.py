"""Tests for connectors/bigquery/access.py — the BqAccess facade."""
import pytest


class TestBqProjects:
    def test_bq_projects_is_frozen_dataclass(self):
        from connectors.bigquery.access import BqProjects
        p = BqProjects(billing="b", data="d")
        assert p.billing == "b"
        assert p.data == "d"
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            p.billing = "other"


class TestBqAccessError:
    def test_carries_kind_message_details(self):
        from connectors.bigquery.access import BqAccessError
        e = BqAccessError("my_kind", "boom", {"foo": "bar"})
        assert e.kind == "my_kind"
        assert e.message == "boom"
        assert e.details == {"foo": "bar"}
        assert str(e) == "boom"

    def test_default_details_is_empty_dict(self):
        from connectors.bigquery.access import BqAccessError
        e = BqAccessError("k", "m")
        assert e.details == {}

    def test_http_status_map_covers_all_kinds(self):
        from connectors.bigquery.access import BqAccessError
        expected = {
            "not_configured": 500,
            "bq_lib_missing": 500,
            "auth_failed": 502,
            "cross_project_forbidden": 502,
            "bq_forbidden": 502,
            "bq_bad_request": 400,
            "bq_upstream_error": 502,
        }
        assert BqAccessError.HTTP_STATUS == expected


class TestTranslateBqError:
    def setup_method(self):
        from connectors.bigquery.access import BqProjects
        self.projects = BqProjects(billing="bill", data="data")

    def test_passes_through_BqAccessError(self):
        """CRITICAL: bq.client() / bq.duckdb_session() raise BqAccessError directly
        for bq_lib_missing / auth_failed. translate_bq_error must pass them through,
        not reclassify as 'unknown' and re-raise."""
        from connectors.bigquery.access import BqAccessError, translate_bq_error
        original = BqAccessError("bq_lib_missing", "no google lib")
        result = translate_bq_error(original, self.projects, bad_request_status="client_error")
        assert result is original

    def test_forbidden_serviceusage_to_cross_project(self):
        from google.api_core.exceptions import Forbidden
        from connectors.bigquery.access import translate_bq_error
        e = Forbidden("Permission denied: serviceusage.services.use on project foo")
        result = translate_bq_error(e, self.projects, bad_request_status="client_error")
        assert result.kind == "cross_project_forbidden"
        assert "billing_project" in result.details
        assert "hint" in result.details

    def test_forbidden_no_serviceusage_to_bq_forbidden(self):
        from google.api_core.exceptions import Forbidden
        from connectors.bigquery.access import translate_bq_error
        e = Forbidden("Permission denied on table-level ACL")
        result = translate_bq_error(e, self.projects, bad_request_status="client_error")
        assert result.kind == "bq_forbidden"

    def test_forbidden_diff_projects_no_serviceusage_still_bq_forbidden(self):
        """billing != data is the NORMAL cross-project setup, not a signal of failure.
        Heuristic must rely on 'serviceusage' substring only."""
        from google.api_core.exceptions import Forbidden
        from connectors.bigquery.access import translate_bq_error, BqProjects
        e = Forbidden("Permission denied on table-level ACL")
        result = translate_bq_error(e, BqProjects(billing="b", data="d"),
                                     bad_request_status="client_error")
        assert result.kind == "bq_forbidden"  # NOT cross_project_forbidden

    def test_bad_request_client_error_to_bq_bad_request_400(self):
        from google.api_core.exceptions import BadRequest
        from connectors.bigquery.access import translate_bq_error, BqAccessError
        e = BadRequest("Syntax error at line 1")
        result = translate_bq_error(e, self.projects, bad_request_status="client_error")
        assert result.kind == "bq_bad_request"
        assert BqAccessError.HTTP_STATUS[result.kind] == 400

    def test_bad_request_upstream_error_to_bq_upstream_error_502(self):
        from google.api_core.exceptions import BadRequest
        from connectors.bigquery.access import translate_bq_error, BqAccessError
        e = BadRequest("malformed identifier")
        result = translate_bq_error(e, self.projects, bad_request_status="upstream_error")
        assert result.kind == "bq_upstream_error"
        assert BqAccessError.HTTP_STATUS[result.kind] == 502

    def test_other_google_api_error_to_bq_upstream_error(self):
        from google.api_core.exceptions import InternalServerError
        from connectors.bigquery.access import translate_bq_error
        e = InternalServerError("BQ borked")
        result = translate_bq_error(e, self.projects, bad_request_status="client_error")
        assert result.kind == "bq_upstream_error"

    def test_unknown_exception_reraises(self):
        from connectors.bigquery.access import translate_bq_error
        with pytest.raises(RuntimeError, match="oops"):
            translate_bq_error(RuntimeError("oops"), self.projects,
                               bad_request_status="client_error")


class TestDefaultClientFactory:
    def test_constructs_client_with_billing_project_as_quota(self, monkeypatch):
        """quota_project_id must be projects.billing, NOT projects.data."""
        from connectors.bigquery.access import _default_client_factory, BqProjects

        captured = {}

        class FakeClientOptions:
            def __init__(self, **kwargs):
                captured["client_options_kwargs"] = kwargs

        class FakeClient:
            def __init__(self, project, client_options):
                captured["project"] = project
                captured["client_options"] = client_options

        import google.cloud.bigquery as bq_mod
        import google.api_core.client_options as co_mod
        monkeypatch.setattr(bq_mod, "Client", FakeClient)
        monkeypatch.setattr(co_mod, "ClientOptions", FakeClientOptions)

        _default_client_factory(BqProjects(billing="bill", data="data"))

        assert captured["project"] == "bill"
        assert captured["client_options_kwargs"]["quota_project_id"] == "bill"

    def test_raises_bq_lib_missing_on_importerror(self, monkeypatch):
        """If google-cloud-bigquery is not installed, raise BqAccessError, not ImportError."""
        from connectors.bigquery.access import _default_client_factory, BqProjects, BqAccessError
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "google.cloud" or name.startswith("google.cloud.bigquery"):
                raise ImportError("no google-cloud-bigquery")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(BqAccessError) as exc_info:
            _default_client_factory(BqProjects(billing="b", data="d"))
        assert exc_info.value.kind == "bq_lib_missing"

    def test_raises_auth_failed_on_default_credentials_error(self, monkeypatch):
        """bigquery.Client(...) resolves ADC at construction; missing credentials in
        CI / dev raise google.auth.exceptions.DefaultCredentialsError synchronously.
        Must translate to BqAccessError(auth_failed), not propagate raw."""
        from connectors.bigquery.access import _default_client_factory, BqProjects, BqAccessError
        from google.auth.exceptions import DefaultCredentialsError

        class FakeClient:
            def __init__(self, project, client_options):
                raise DefaultCredentialsError("no ADC")

        import google.cloud.bigquery as bq_mod
        monkeypatch.setattr(bq_mod, "Client", FakeClient)

        with pytest.raises(BqAccessError) as exc_info:
            _default_client_factory(BqProjects(billing="b", data="d"))
        assert exc_info.value.kind == "auth_failed"
        assert "no ADC" in exc_info.value.message
        assert "hint" in exc_info.value.details


class TestDefaultDuckdbSessionFactory:
    def test_yields_duckdb_conn_with_secret_then_closes(self, monkeypatch):
        from connectors.bigquery.access import _default_duckdb_session_factory, BqProjects

        executed_sql = []

        class FakeConn:
            def __init__(self):
                self.closed = False
            def execute(self, sql, params=None):
                executed_sql.append((sql, params))
                return self
            def close(self):
                self.closed = True

        fake_conn = FakeConn()
        monkeypatch.setattr("duckdb.connect", lambda _: fake_conn)
        monkeypatch.setattr("connectors.bigquery.auth.get_metadata_token", lambda: "tok123")

        with _default_duckdb_session_factory(BqProjects(billing="b", data="d")) as conn:
            assert conn is fake_conn
        assert fake_conn.closed is True

        # Verify INSTALL/LOAD/SECRET sequence ran
        assert any("INSTALL bigquery" in sql for sql, _ in executed_sql)
        assert any("LOAD bigquery" in sql for sql, _ in executed_sql)
        assert any("CREATE OR REPLACE SECRET" in sql and "tok123" in sql for sql, _ in executed_sql)

    def test_closes_on_exception_inside_with_block(self, monkeypatch):
        from connectors.bigquery.access import _default_duckdb_session_factory, BqProjects

        class FakeConn:
            closed = False
            def execute(self, *a, **kw): return self
            def close(self): self.closed = True

        fake_conn = FakeConn()
        monkeypatch.setattr("duckdb.connect", lambda _: fake_conn)
        monkeypatch.setattr("connectors.bigquery.auth.get_metadata_token", lambda: "t")

        with pytest.raises(RuntimeError, match="boom"):
            with _default_duckdb_session_factory(BqProjects(billing="b", data="d")) as conn:
                raise RuntimeError("boom")
        assert fake_conn.closed is True

    def test_translates_metadata_auth_error_to_auth_failed(self, monkeypatch):
        from connectors.bigquery.access import _default_duckdb_session_factory, BqProjects, BqAccessError
        from connectors.bigquery.auth import BQMetadataAuthError

        def fail():
            raise BQMetadataAuthError("metadata server unreachable")

        monkeypatch.setattr("connectors.bigquery.auth.get_metadata_token", fail)

        with pytest.raises(BqAccessError) as exc_info:
            with _default_duckdb_session_factory(BqProjects(billing="b", data="d")):
                pass
        assert exc_info.value.kind == "auth_failed"


class TestBqAccess:
    def test_uses_default_factories_when_none_passed(self, monkeypatch):
        from connectors.bigquery.access import BqAccess, BqProjects

        captured = []
        monkeypatch.setattr(
            "connectors.bigquery.access._default_client_factory",
            lambda projects: captured.append(("client", projects)) or "FAKE_CLIENT",
        )
        bq = BqAccess(BqProjects(billing="b", data="d"))
        assert bq.client() == "FAKE_CLIENT"
        assert captured == [("client", BqProjects(billing="b", data="d"))]

    def test_injected_client_factory_overrides_default(self):
        from connectors.bigquery.access import BqAccess, BqProjects
        bq = BqAccess(
            BqProjects(billing="b", data="d"),
            client_factory=lambda projects: "MOCK_CLIENT",
        )
        assert bq.client() == "MOCK_CLIENT"

    def test_injected_duckdb_session_factory_overrides_default(self):
        from connectors.bigquery.access import BqAccess, BqProjects
        from contextlib import contextmanager

        @contextmanager
        def fake_session(projects):
            yield "FAKE_CONN"

        bq = BqAccess(
            BqProjects(billing="b", data="d"),
            duckdb_session_factory=fake_session,
        )
        with bq.duckdb_session() as conn:
            assert conn == "FAKE_CONN"

    def test_projects_property(self):
        from connectors.bigquery.access import BqAccess, BqProjects
        p = BqProjects(billing="b", data="d")
        bq = BqAccess(p)
        assert bq.projects is p


class TestGetBqAccess:
    def setup_method(self):
        # Clear the cache between tests
        from connectors.bigquery.access import get_bq_access
        get_bq_access.cache_clear()

    def test_env_var_wins(self, monkeypatch):
        from connectors.bigquery.access import get_bq_access
        monkeypatch.setenv("BIGQUERY_PROJECT", "env-proj")
        bq = get_bq_access()
        assert bq.projects.billing == "env-proj"
        assert bq.projects.data == "env-proj"

    def test_billing_project_from_yaml_when_no_env(self, monkeypatch):
        from connectors.bigquery.access import get_bq_access
        monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)

        def fake_get_value(*keys, default=""):
            return {
                ("data_source", "bigquery", "billing_project"): "yaml-bill",
                ("data_source", "bigquery", "project"): "yaml-data",
            }.get(keys, default)

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        bq = get_bq_access()
        assert bq.projects.billing == "yaml-bill"
        assert bq.projects.data == "yaml-data"

    def test_billing_falls_back_to_project_when_no_billing(self, monkeypatch):
        from connectors.bigquery.access import get_bq_access
        monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)

        def fake_get_value(*keys, default=""):
            return {
                ("data_source", "bigquery", "project"): "yaml-data",
            }.get(keys, default)

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        bq = get_bq_access()
        assert bq.projects.billing == "yaml-data"
        assert bq.projects.data == "yaml-data"

    def test_returns_sentinel_when_neither_set(self, monkeypatch):
        """get_bq_access() MUST NOT raise during dep-injection on non-BQ instances —
        that would 500 every v2 endpoint request even for local-source tables.
        Returns a sentinel BqAccess whose client() / duckdb_session() raise
        BqAccessError(not_configured) only when actually called. The endpoint's
        try/except BqAccessError catches that path normally. Devin BUG_0001 on
        PR #138 review."""
        from connectors.bigquery.access import get_bq_access, BqAccessError, BqAccess
        monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default="": default)

        bq = get_bq_access()
        assert isinstance(bq, BqAccess)

        with pytest.raises(BqAccessError) as exc_info:
            bq.client()
        assert exc_info.value.kind == "not_configured"
        assert "billing_project" in exc_info.value.details["hint"].lower() or \
               "project" in exc_info.value.details["hint"].lower()

        # duckdb_session() is a context manager; the BqAccessError must surface on __enter__
        with pytest.raises(BqAccessError) as exc_info:
            with bq.duckdb_session():
                pass
        assert exc_info.value.kind == "not_configured"

    def test_is_cached(self, monkeypatch):
        from connectors.bigquery.access import get_bq_access
        monkeypatch.setenv("BIGQUERY_PROJECT", "p")
        a = get_bq_access()
        b = get_bq_access()
        assert a is b

    def test_sentinel_is_cached_per_process(self, monkeypatch):
        """The sentinel BqAccess is cached like any other return value. Operators
        fixing instance.yaml at runtime must restart the container to pick up the
        change — documented as expected behavior in the spec ('Hot-reload of
        instance.yaml is out of scope')."""
        from connectors.bigquery.access import get_bq_access, BqAccess
        monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default="": default)

        a = get_bq_access()
        b = get_bq_access()
        assert a is b
        assert isinstance(a, BqAccess)
        assert a.projects.billing == ""
