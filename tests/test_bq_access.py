"""Tests for connectors/bigquery/access.py — the BqAccess facade."""
import pytest
import threading


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
            # User-facing class for "Response too large to return" — an
            # upstream BQ refusal, but caused by query shape (too many rows
            # to fit in a single jobs.query response) rather than auth or
            # syntax. 400 so the user sees an actionable error and not a
            # 502 that suggests "BQ is broken".
            "bq_response_too_large": 400,
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

    def test_duckdb_native_forbidden_classified_via_string_match(self):
        """The DuckDB bigquery extension is a C++ plugin making its own HTTP
        calls; BQ 403 arrives as duckdb.IOException with 'Forbidden' / '403'
        in the message, NOT as gax.Forbidden. Last-resort heuristic must
        classify these so /scan, /sample, /schema don't fall back to bare 500
        in production. Devin ANALYSIS on PR #138 review."""
        from connectors.bigquery.access import translate_bq_error
        # Simulate what duckdb.IOException looks like — a plain Exception with
        # the BQ error text embedded by the C++ extension's HTTP layer.
        e = Exception("HTTP 403 Forbidden: serviceusage.services.use denied on project x")
        result = translate_bq_error(e, self.projects, bad_request_status="upstream_error")
        assert result.kind == "cross_project_forbidden"
        assert "billing_project" in result.details

    def test_duckdb_native_forbidden_non_serviceusage(self):
        from connectors.bigquery.access import translate_bq_error
        e = Exception("HTTP 403: User does not have permission to access table foo")
        result = translate_bq_error(e, self.projects, bad_request_status="upstream_error")
        assert result.kind == "bq_forbidden"

    def test_duckdb_native_bad_request_classified_via_string_match(self):
        from connectors.bigquery.access import translate_bq_error
        e = Exception("400 Bad Request: Syntax error at line 1")
        result = translate_bq_error(e, self.projects, bad_request_status="client_error")
        assert result.kind == "bq_bad_request"

    def test_unknown_exception_without_bq_pattern_still_reraises(self):
        """Heuristic must be specific — random exceptions without HTTP-error
        keywords still re-raise (don't swallow programmer bugs)."""
        from connectors.bigquery.access import translate_bq_error
        with pytest.raises(ValueError, match="not a BQ error"):
            translate_bq_error(ValueError("not a BQ error"), self.projects,
                               bad_request_status="client_error")

    def test_response_too_large_via_gax_bad_request(self):
        """BQ ``responseTooLarge`` arrives as ``gax.BadRequest`` (HTTP 400
        with a specific `reason` field). Pre-fix this fell through to the
        generic ``bq_bad_request`` mapping — surfacing as a 400 with the
        raw upstream message and no actionable hint. Now it routes to a
        dedicated ``bq_response_too_large`` kind whose message tells the
        user exactly what to do (narrow WHERE / aggregate / use materialized).
        """
        from google.api_core.exceptions import BadRequest
        from connectors.bigquery.access import translate_bq_error
        e = BadRequest("Response too large to return. Consider setting allowLargeResults to true ...")
        result = translate_bq_error(
            e, self.projects, bad_request_status="client_error",
        )
        assert result.kind == "bq_response_too_large", (
            f"got {result.kind!r}; expected dedicated mapping for "
            "'Response too large' to avoid the generic bq_bad_request 400 "
            "with no actionable hint"
        )
        # User-facing message must point at the actionable remediations,
        # not just echo the raw BQ string.
        assert "exceeded" in result.message.lower() or "too large" in result.message.lower()
        assert "where" in result.message.lower() or "aggregate" in result.message.lower() or "materialized" in result.message.lower()
        # Original upstream text preserved in details for operator debugging.
        assert "original" in result.details
        assert "Response too large" in result.details["original"]

    def test_response_too_large_via_duckdb_native_string(self):
        """DuckDB-native exceptions (the BQ extension's C++ HTTP path)
        carry the same 'Response too large' marker in plain ``Exception``
        messages — must classify the same way as the gax.BadRequest case."""
        from connectors.bigquery.access import translate_bq_error
        e = Exception("HTTP 400: Response too large to return.")
        result = translate_bq_error(
            e, self.projects, bad_request_status="upstream_error",
        )
        assert result.kind == "bq_response_too_large"

    def test_response_too_large_classification_is_status_independent(self):
        """The mapping must fire regardless of ``bad_request_status``
        (some callers route via 'upstream_error', others via 'client_error').
        It's the BQ error shape that matters, not who's calling."""
        from google.api_core.exceptions import BadRequest
        from connectors.bigquery.access import translate_bq_error
        e = BadRequest("Response too large to return")
        for status in ("client_error", "upstream_error"):
            result = translate_bq_error(e, self.projects, bad_request_status=status)
            assert result.kind == "bq_response_too_large", (
                f"bad_request_status={status!r} routed to {result.kind!r}; "
                "expected bq_response_too_large for both"
            )

    def test_response_too_large_does_not_trigger_on_unrelated_bad_request(self):
        """Other BadRequests (syntax errors, malformed identifiers, …)
        must keep going through the generic bq_bad_request mapping — only
        the 'Response too large' substring triggers the dedicated kind."""
        from google.api_core.exceptions import BadRequest
        from connectors.bigquery.access import translate_bq_error
        e = BadRequest("Syntax error at [1:23] near unexpected token")
        result = translate_bq_error(
            e, self.projects, bad_request_status="client_error",
        )
        assert result.kind == "bq_bad_request"


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
    def test_yields_duckdb_conn_with_secret_set_via_pool(self, monkeypatch):
        """The pool's first acquire on an empty pool runs the full
        INSTALL/LOAD/SECRET sequence. After the with-block exits the
        connection is RETURNED to the pool (not closed) so the next
        acquire amortizes the extension-load cost.

        Pre-pool semantics (close-on-exit) are preserved on broken
        entries + on the explicit pool-reset path; covered in
        TestBqSessionPool.
        """
        from connectors.bigquery.access import (
            _default_duckdb_session_factory, BqProjects,
            _reset_session_pool_for_tests,
        )
        _reset_session_pool_for_tests()

        executed_sql = []

        class FakeConn:
            def __init__(self):
                self.closed = False
            def execute(self, sql, params=None):
                executed_sql.append((sql, params))
                class _Result:
                    def fetchone(self_inner):
                        return (1,)
                return _Result()
            def close(self):
                self.closed = True

        fake_conn = FakeConn()
        monkeypatch.setattr("duckdb.connect", lambda _: fake_conn)
        monkeypatch.setattr("connectors.bigquery.auth.get_metadata_token", lambda: "tok123")

        with _default_duckdb_session_factory(BqProjects(billing="b", data="d")) as conn:
            assert conn is fake_conn
        # Pool retains the conn — close happens at pool reset / shutdown.
        assert fake_conn.closed is False

        # Verify INSTALL/LOAD/SECRET sequence ran
        assert any("INSTALL bigquery" in sql for sql, _ in executed_sql)
        assert any("LOAD bigquery" in sql for sql, _ in executed_sql)
        assert any("CREATE OR REPLACE SECRET" in sql and "tok123" in sql for sql, _ in executed_sql)

        # Explicit pool reset closes the retained entry.
        _reset_session_pool_for_tests()
        assert fake_conn.closed is True

    def test_closes_on_exception_inside_with_block(self, monkeypatch):
        """Exceptions inside the with-block leave the underlying conn in
        an unknown state (half-completed query, dirty session); the pool
        treats it as broken and closes it rather than returning to pool.
        """
        from connectors.bigquery.access import (
            _default_duckdb_session_factory, BqProjects,
            _reset_session_pool_for_tests,
        )
        _reset_session_pool_for_tests()

        class FakeConn:
            closed = False
            def execute(self, *a, **kw):
                class _Result:
                    def fetchone(self_inner):
                        return (1,)
                return _Result()
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

    def test_fetch_helpers_raise_not_configured_on_sentinel_before_identifier_validation(self, monkeypatch):
        """Sentinel BqAccess has BqProjects(data=""). v2 fetch helpers must trigger
        bq.client() (which raises BqAccessError(not_configured)) BEFORE calling
        validate_quoted_identifier on the empty string. Otherwise the operator
        sees a confusing HTTP 400 'unsafe_identifier' instead of the intended
        HTTP 500 'not_configured' with hint. Devin BUG_0002 on PR #138 review."""
        from connectors.bigquery.access import get_bq_access, BqAccessError
        from app.api.v2_sample import _fetch_bq_sample
        from app.api.v2_schema import _fetch_bq_schema, _fetch_bq_table_options

        monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
        monkeypatch.setattr("app.instance_config.get_value", lambda *k, default="": default)
        bq = get_bq_access()
        assert bq.projects.data == "", "must be the sentinel"

        # Strict paths surface BqAccessError(not_configured), NOT ValueError(unsafe).
        with pytest.raises(BqAccessError) as exc_info:
            _fetch_bq_sample(bq, "ds", "tbl", 5)
        assert exc_info.value.kind == "not_configured"

        with pytest.raises(BqAccessError) as exc_info:
            _fetch_bq_schema(bq, "ds", "tbl")
        assert exc_info.value.kind == "not_configured"

        # Best-effort path returns {} silently.
        assert _fetch_bq_table_options(bq, "ds", "tbl") == {}

    def test_instance_config_reset_cache_invalidates_get_bq_access(self, monkeypatch):
        """admin /api/admin/server-config save → instance_config.reset_cache() →
        must also clear get_bq_access cache so v2 endpoints pick up new
        BigQuery project IDs without container restart. Devin ANALYSIS_0004
        on PR #138 review: pre-Phase-2 each request re-read get_value(), so
        admin hot-reload worked. functools.cache on get_bq_access would have
        broken that contract — this test guards against regressing it."""
        from connectors.bigquery.access import get_bq_access
        from app.instance_config import reset_cache

        monkeypatch.setenv("BIGQUERY_PROJECT", "first")
        bq1 = get_bq_access()
        assert bq1.projects.billing == "first"

        # Operator updates config and triggers reset_cache via admin API
        monkeypatch.setenv("BIGQUERY_PROJECT", "second")
        reset_cache()

        bq2 = get_bq_access()
        assert bq2.projects.billing == "second", \
            "get_bq_access must re-resolve after instance_config.reset_cache()"
        assert bq2 is not bq1

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


# ---------------------------------------------------------------------------
# DuckDB BQ-extension session pool — amortizes the ~0.5 s INSTALL/LOAD/ATTACH
# cost across requests by keeping pre-warmed DuckDB connections in a
# bounded pool. Each acquire reuses an existing connection (refreshing the
# auth SECRET so token rotation doesn't break long-lived entries) instead
# of spinning up a fresh DuckDB+extension load every time.
# ---------------------------------------------------------------------------


class _PoolFakeConn:
    """Fake DuckDB connection that records executed SQL and supports
    ``close()``. Used across pool tests so we can pin behavior without
    booting the real BigQuery extension."""
    _serial = 0

    def __init__(self):
        type(self)._serial += 1
        self.id = type(self)._serial
        self.closed = False
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        # Liveness probe: SELECT 1 returns something fetchable.
        class _Result:
            def fetchone(self_inner):
                return (1,)
            def fetchall(self_inner):
                return [(1,)]
        return _Result()

    def close(self):
        self.closed = True


@pytest.fixture
def reset_pool(monkeypatch):
    """Reset the BQ session pool singleton between tests so leak-detection
    assertions don't carry state."""
    from connectors.bigquery import access as bq_access_mod
    if hasattr(bq_access_mod, "_reset_session_pool_for_tests"):
        bq_access_mod._reset_session_pool_for_tests()
    monkeypatch.setattr(
        "connectors.bigquery.auth.get_metadata_token",
        lambda: "tok-pool",
    )
    yield
    if hasattr(bq_access_mod, "_reset_session_pool_for_tests"):
        bq_access_mod._reset_session_pool_for_tests()


class TestBqSessionPool:
    def test_pool_reuses_connections_across_acquires(self, monkeypatch, reset_pool):
        """Acquiring a session, releasing, then acquiring again must return
        the SAME underlying DuckDB connection — no INSTALL/LOAD overhead on
        the second request. This is the whole point of the pool."""
        from connectors.bigquery.access import _default_duckdb_session_factory, BqProjects

        # Each duckdb.connect() yields a fresh _PoolFakeConn so we can tell
        # them apart by id.
        connections_made = []
        def fake_connect(_path):
            c = _PoolFakeConn()
            connections_made.append(c)
            return c
        monkeypatch.setattr("duckdb.connect", fake_connect)

        # First acquire: pool is empty, factory builds a new entry.
        with _default_duckdb_session_factory(BqProjects(billing="b", data="d")) as conn1:
            id1 = conn1.id

        # Second acquire: pool has a warm entry, must hand back the same conn.
        with _default_duckdb_session_factory(BqProjects(billing="b", data="d")) as conn2:
            id2 = conn2.id

        assert id1 == id2, (
            "expected the same pooled connection across two acquires; "
            f"got id1={id1}, id2={id2}"
        )
        # And we must NOT have re-INSTALLed/LOADed the extension on reuse —
        # only one duckdb.connect() call ever happened.
        assert len(connections_made) == 1, (
            f"pool re-built the conn on second acquire; created {len(connections_made)}"
        )

    def test_pool_size_is_configurable(self, monkeypatch, reset_pool):
        """``data_source.bigquery.session_pool_size`` controls the upper
        bound on warm entries. Above the cap, releasing extra entries
        closes them rather than retaining."""
        from connectors.bigquery.access import _default_duckdb_session_factory, BqProjects

        def fake_get_value(*keys, default=None):
            if keys == ("data_source", "bigquery", "session_pool_size"):
                return 2  # tiny pool
            if keys == ("data_source", "bigquery", "query_timeout_ms"):
                return 0  # don't try to SET timeout in tests
            return default

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        monkeypatch.setattr("duckdb.connect", lambda _: _PoolFakeConn())

        # Acquire 3 in parallel to force 3 simultaneous entries.
        cm1 = _default_duckdb_session_factory(BqProjects(billing="b", data="d"))
        c1 = cm1.__enter__()
        cm2 = _default_duckdb_session_factory(BqProjects(billing="b", data="d"))
        c2 = cm2.__enter__()
        cm3 = _default_duckdb_session_factory(BqProjects(billing="b", data="d"))
        c3 = cm3.__enter__()

        # Release all three. The 3rd release should close the conn since
        # the pool already has 2.
        cm1.__exit__(None, None, None)
        cm2.__exit__(None, None, None)
        cm3.__exit__(None, None, None)

        # At least one of the three connections must be closed (pool overflow).
        closed_count = sum(1 for c in (c1, c2, c3) if c.closed)
        assert closed_count >= 1, (
            "pool retained more than its configured size; expected at least "
            f"one close. closed_count={closed_count}"
        )
        # Pool retained at most `size` entries, so total live + closed = 3,
        # closed >= 1 means pool size <= 2.
        assert closed_count == 1

    def test_pool_replaces_broken_connection(self, monkeypatch, reset_pool):
        """If a pooled entry's liveness check fails on acquire (the
        underlying DuckDB conn was closed externally, BQ extension state
        corrupted, etc.), the pool must drop it and build a fresh entry —
        not hand the broken one to the caller."""
        from connectors.bigquery.access import _default_duckdb_session_factory, BqProjects

        # First acquire creates entry #1; we'll then mark it broken.
        all_conns: list[_PoolFakeConn] = []
        def fake_connect(_path):
            c = _PoolFakeConn()
            all_conns.append(c)
            return c
        monkeypatch.setattr("duckdb.connect", fake_connect)

        with _default_duckdb_session_factory(BqProjects(billing="b", data="d")) as conn1:
            id1 = conn1.id
            # Simulate corruption: make execute() raise on next call.
            def broken_execute(*a, **kw):
                raise RuntimeError("connection broken")
            conn1.execute = broken_execute  # type: ignore[assignment]

        # Second acquire must skip the broken entry and build a fresh one.
        with _default_duckdb_session_factory(BqProjects(billing="b", data="d")) as conn2:
            id2 = conn2.id

        assert id1 != id2, (
            f"expected a fresh conn after broken-pool reaper; both acquires "
            f"returned id={id1}"
        )
        assert len(all_conns) >= 2

    def test_pool_handles_reentrant_acquires_thread_safe(self, monkeypatch, reset_pool):
        """Concurrent acquires from multiple threads must never hand the
        same underlying DuckDB conn to two threads at once. The pool's
        lock acquires/releases are the load-bearing invariant here.
        """
        from connectors.bigquery.access import _default_duckdb_session_factory, BqProjects

        monkeypatch.setattr("duckdb.connect", lambda _: _PoolFakeConn())

        active_ids: set = set()
        active_lock = threading.Lock()
        violations: list = []

        def worker():
            for _ in range(20):
                with _default_duckdb_session_factory(
                    BqProjects(billing="b", data="d"),
                ) as conn:
                    with active_lock:
                        if conn.id in active_ids:
                            violations.append(conn.id)
                        active_ids.add(conn.id)
                    # Hold briefly to give other threads a chance to race.
                    time.sleep(0.001)
                    with active_lock:
                        active_ids.discard(conn.id)

        import time
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not violations, (
            f"pool handed the same conn to multiple threads concurrently: "
            f"{violations}"
        )

    def test_pool_does_not_apply_when_factory_is_injected(self, monkeypatch, reset_pool):
        """Test fixtures that inject a custom ``duckdb_session_factory``
        (e.g. tests/conftest.py's ``bq_access`` fixture) MUST bypass the
        pool entirely — otherwise their nullcontext-wrapped fake would
        get retained between tests and corrupt downstream assertions.
        """
        from connectors.bigquery.access import BqAccess, BqProjects
        from contextlib import contextmanager

        sentinel = object()

        @contextmanager
        def custom_factory(_projects):
            yield sentinel

        bq = BqAccess(
            BqProjects(billing="b", data="d"),
            duckdb_session_factory=custom_factory,
        )
        with bq.duckdb_session() as conn:
            assert conn is sentinel


# ---- apply_bq_session_settings resource caps (#431 follow-up / #432) -------

class TestApplyBqSessionSettings:
    def test_apply_bq_session_settings_applies_resource_caps(self):
        """``apply_bq_session_settings`` must apply the three core DuckDB
        resource caps UNCONDITIONALLY — before the BQ-extension-setting
        early-exit and on every pool acquire. The caps are core DuckDB, so
        this works hermetically on a plain in-memory conn with no BQ
        extension loaded (the BQ-only ``bq_query_timeout_ms`` SET logs and
        does not raise, which this test tolerates).
        """
        import duckdb
        from connectors.bigquery.access import apply_bq_session_settings

        conn = duckdb.connect(":memory:")
        try:
            # Capture the default memory_limit BEFORE applying the caps — the
            # DuckDB default is 80% of host RAM, meaningfully larger than the
            # 2 GiB cap. Proving BEFORE != AFTER is the regression guard for
            # the "caps run before the extension early-exit" lift.
            before = conn.execute(
                "SELECT current_setting('memory_limit')"
            ).fetchone()[0]

            apply_bq_session_settings(conn)

            threads = conn.execute(
                "SELECT current_setting('threads')"
            ).fetchone()[0]
            assert int(threads) == 2

            preserve = conn.execute(
                "SELECT current_setting('preserve_insertion_order')"
            ).fetchone()[0]
            assert preserve in (False, "false")

            after = conn.execute(
                "SELECT current_setting('memory_limit')"
            ).fetchone()[0]
            # Normalized/banded assertion: '2GB' -> '1.8 GiB'. An exact
            # string compare would false-fail.
            assert "GiB" in after, after
            assert float(after.split()[0]) <= 2.0, after

            # The cap was actually applied: the post-apply value differs from
            # the (much larger) default. On the rare host where the default
            # already happens to be <= 2 GiB this still holds because the
            # band assertion above pins the result regardless.
            assert before != after, (before, after)
        finally:
            conn.close()
