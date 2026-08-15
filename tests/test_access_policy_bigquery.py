"""Task 10 -- the BigQuery arm of table access policies (design doc §7):
transpile, named parameters, ordering, fail-closed.

Three things, each a class below:

(a) ``policied_relation(dialect="bigquery")`` transpiles the policy body to
    BigQuery Standard SQL (§7.2) -- direct-repository level, mirroring
    ``tests/test_access_policy_resolver.py``.
(b) ``run_bq_query_to_arrow`` threads ``query_parameters`` into the BQ job
    config (§7.1), and ``bq_query_parameters_from_policy_params`` builds the
    right parameter shape from a resolver's ``.params`` dict -- mocking the
    BQ client the same way ``tests/test_remote_select_bq_labels.py`` does.
(c) Fail-closed end to end: when the BQ execution for a policied
    ``query_mode='remote'`` table raises, ``/api/query`` must NOT fall back
    to an unfiltered execution (§7.4/§17) -- the response is an error, not
    200-with-rows, and the DuckDB-side analytics connection is never
    touched at all for that request.
"""

from __future__ import annotations

import pytest

POLICY_SQL = (
    "SELECT * EXCLUDE (national_id), md5(email) AS email FROM invoices WHERE list_contains($user_groups, cost_center)"
)

REMOTE_POLICY_SQL = "SELECT * FROM invoices WHERE list_contains($user_groups, unit)"


# ---------------------------------------------------------------------------
# (a) Transpile arm
# ---------------------------------------------------------------------------


@pytest.fixture
def transpile_env(e2e_env):
    """A solo, non-admin user with a policied table, seeded directly
    through the repositories -- no HTTP client needed for this arm."""
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository
    from src.db import get_system_db

    conn = get_system_db()
    users = UserRepository(conn)
    users.create(id="u_solo", email="solo@example.com", name="Solo")

    groups = UserGroupsRepository(conn)
    finance_gid = groups.create(name="Finance")["id"]
    members = UserGroupMembersRepository(conn)
    members.add_member("u_solo", finance_gid, source="admin")

    registry = TableRegistryRepository(conn)
    registry.register(
        id="tbl_invoices",
        name="invoices",
        source_type="keboola",
        query_mode="local",
        server_only=True,
    )
    registry.set_access_policy("tbl_invoices", sql=POLICY_SQL, note="cost-centre filter", updated_by="admin")
    conn.close()

    return {"id": "u_solo", "email": "solo@example.com"}


class TestBigQueryTranspile:
    """(a) `policied_relation(dialect="bigquery")` transpiles the policy
    body -- verified against the SAME transformations the design doc's
    §7.2 worked example claims, on this repo's pinned sqlglot version."""

    def test_exclude_becomes_except(self, transpile_env):
        from src.access_policy import policied_relation

        result = policied_relation("tbl_invoices", transpile_env, dialect="bigquery")
        assert "EXCLUDE" not in result.relation_sql
        assert "EXCEPT" in result.relation_sql

    def test_md5_becomes_to_hex_md5(self, transpile_env):
        from src.access_policy import policied_relation

        result = policied_relation("tbl_invoices", transpile_env, dialect="bigquery")
        assert "TO_HEX(MD5(" in result.relation_sql

    def test_dollar_user_groups_becomes_at_user_groups(self, transpile_env):
        from src.access_policy import policied_relation

        result = policied_relation("tbl_invoices", transpile_env, dialect="bigquery")
        assert "$user_groups" not in result.relation_sql
        assert "@user_groups" in result.relation_sql

    def test_policied_true_and_params_are_the_same_identity_values_as_duckdb_arm(self, transpile_env):
        """§7.2: one authored policy, same bind VALUES on both dialects --
        only the SQL text differs, never the Python params dict shape."""
        from src.access_policy import policied_relation

        duckdb_result = policied_relation("tbl_invoices", transpile_env, dialect="duckdb")
        bq_result = policied_relation("tbl_invoices", transpile_env, dialect="bigquery")

        assert bq_result.policied is True
        assert bq_result.table_id == "tbl_invoices"
        assert bq_result.params == duckdb_result.params
        assert bq_result.relation_sql != duckdb_result.relation_sql

    def test_admin_bypass_holds_on_the_bigquery_dialect_too(self, seeded_app, transpile_env):
        """§12's admin bypass is identity resolution, not dialect-specific
        -- an admin gets the SAME unfiltered passthrough on either arm."""
        from src.access_policy import policied_relation
        from src.db import get_system_db
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.db import SYSTEM_ADMIN_GROUP

        conn = get_system_db()
        try:
            admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
            UserGroupMembersRepository(conn).add_member("u_admin_bq", admin_gid, source="system_seed")
            from src.repositories.users import UserRepository

            UserRepository(conn).create(id="u_admin_bq", email="admin-bq@example.com", name="Admin BQ")
        finally:
            conn.close()

        result = policied_relation(
            "tbl_invoices", {"id": "u_admin_bq", "email": "admin-bq@example.com"}, dialect="bigquery"
        )
        assert result.policied is False
        assert result.params == {}

    def test_a_transpile_failure_is_a_policy_error_not_a_raw_sqlglot_exception(self, e2e_env):
        """§16: an unrecoverable resolution failure is always the SAME
        table-scoped reason code, never an engine-internal exception type
        leaking past the resolver."""
        from src.access_policy import PolicyError, policied_relation
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        from src.repositories.users import UserRepository

        conn = get_system_db()
        UserRepository(conn).create(id="u_x", email="x@example.com", name="X")
        registry = TableRegistryRepository(conn)
        registry.register(id="tbl_bad", name="bad_policy_tbl", source_type="keboola", query_mode="local")
        # Deliberately unparseable-as-SQL text -- save-time validation (Task
        # 3) would reject this at write time; a direct repo write (as this
        # test does, matching every other fixture in this file) bypasses
        # that gate, which is exactly why the resolver -- not only the
        # validator -- must fail closed on a bad body.
        registry.set_access_policy("tbl_bad", sql="NOT VALID SQL (((", note="broken", updated_by="admin")
        conn.close()

        with pytest.raises(PolicyError) as exc_info:
            policied_relation("tbl_bad", {"id": "u_x", "email": "x@example.com"}, dialect="bigquery")
        assert exc_info.value.table_id == "tbl_bad"


# ---------------------------------------------------------------------------
# (b) Named parameters reach the BQ job config
# ---------------------------------------------------------------------------


class TestBqQueryParametersFromPolicyParams:
    """`bq_query_parameters_from_policy_params` builds the BigQuery
    parameter shapes `list_contains($user_groups, ...)`'s transpiled form
    (`EXISTS(SELECT 1 FROM UNNEST(@user_groups) ...)`) expects."""

    def test_scalar_values_become_scalar_query_parameters(self):
        from google.cloud import bigquery

        from connectors.bigquery.access import bq_query_parameters_from_policy_params

        params = bq_query_parameters_from_policy_params({"user_email": "a@example.com", "user_id": "u1"})
        by_name = {p.name: p for p in params}
        assert isinstance(by_name["user_email"], bigquery.ScalarQueryParameter)
        assert by_name["user_email"].value == "a@example.com"
        assert isinstance(by_name["user_id"], bigquery.ScalarQueryParameter)
        assert by_name["user_id"].value == "u1"

    def test_list_values_become_array_query_parameters(self):
        from google.cloud import bigquery

        from connectors.bigquery.access import bq_query_parameters_from_policy_params

        params = bq_query_parameters_from_policy_params({"user_groups": ["Finance", "Marketing"]})
        (p,) = params
        assert isinstance(p, bigquery.ArrayQueryParameter)
        assert p.name == "user_groups"
        assert p.values == ["Finance", "Marketing"]

    def test_empty_params_builds_an_empty_list(self):
        from connectors.bigquery.access import bq_query_parameters_from_policy_params

        assert bq_query_parameters_from_policy_params({}) == []


class TestRunBqQueryToArrowPassesQueryParameters:
    """`run_bq_query_to_arrow` threads `query_parameters` all the way into
    `QueryJobConfig` -- never string-interpolated into the SQL text."""

    def test_query_parameters_reach_the_job_config(self):
        import pyarrow as pa
        from google.cloud import bigquery

        from connectors.bigquery.access import BqAccess, BqProjects, run_bq_query_to_arrow

        captured = {}

        class _FakeJob:
            job_id = "j-1"
            total_bytes_processed = 10
            total_bytes_billed = 12

            def to_arrow(self, create_bqstorage_client=True):
                return pa.table({"c": [1]})

        class _FakeClient:
            def query(self, sql, job_config=None):
                captured["sql"] = sql
                captured["job_config"] = job_config
                return _FakeJob()

        bq = BqAccess(BqProjects(billing="bp", data="dp"), client_factory=lambda projects: _FakeClient())
        query_parameters = [
            bigquery.ScalarQueryParameter("user_email", "STRING", "a@example.com"),
            bigquery.ArrayQueryParameter("user_groups", "STRING", ["Finance"]),
        ]

        table, job_info = run_bq_query_to_arrow(
            bq,
            "SELECT 1 WHERE list_contains(@user_groups, unit)",
            query_parameters=query_parameters,
            labels={"agent_name": "query"},
        )

        assert table.num_rows == 1
        job_config = captured["job_config"]
        assert job_config.query_parameters == query_parameters
        assert job_config.labels == {"agent_name": "query"}
        # Values never got string-interpolated into the SQL text itself.
        assert "Finance" not in captured["sql"]
        assert job_info["bq_job_id"] == "j-1"

    def test_no_query_parameters_defaults_to_an_empty_list_not_none(self):
        """`QueryJobConfig(query_parameters=None)` is a different (invalid)
        shape than `query_parameters=[]` for some client versions -- the
        default must be the empty list, matching every OTHER (non-policied)
        `run_bq_query_to_arrow` caller that never passes this kwarg."""
        import pyarrow as pa

        from connectors.bigquery.access import BqAccess, BqProjects, run_bq_query_to_arrow

        captured = {}

        class _FakeJob:
            job_id = None
            total_bytes_processed = None
            total_bytes_billed = None

            def to_arrow(self, create_bqstorage_client=True):
                return pa.table({"c": [1]})

        class _FakeClient:
            def query(self, sql, job_config=None):
                captured["job_config"] = job_config
                return _FakeJob()

        bq = BqAccess(BqProjects(billing="bp", data="dp"), client_factory=lambda projects: _FakeClient())
        run_bq_query_to_arrow(bq, "SELECT 1", labels={})
        assert captured["job_config"].query_parameters == []


# ---------------------------------------------------------------------------
# (c) Fail-closed end to end via /api/query
# ---------------------------------------------------------------------------


@pytest.fixture
def policied_bq_invoices(seeded_app, monkeypatch):
    """One `query_mode='remote'` BigQuery table carrying `REMOTE_POLICY_SQL`,
    granted to a single non-admin user whose group is also the policy's
    filter value -- mirrors `test_access_policy_query_endpoint.py`'s
    `policied_orders` fixture, but for the remote/BQ arm."""
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="bq.fin.invoices",
            name="invoices",
            source_type="bigquery",
            bucket="fin",
            source_table="invoices",
            query_mode="remote",
        )
        registry.set_access_policy("bq.fin.invoices", sql=REMOTE_POLICY_SQL, note="unit filter", updated_by="admin")

        users = UserRepository(conn)
        users.create(id="u_team_a", email="team-a@example.com", name="Team A")
        grant_table_via_package(conn, "bq.fin.invoices", "u_team_a", group_name="TeamA")
    finally:
        conn.close()

    return {
        **seeded_app,
        "team_a_token": create_access_token("u_team_a", "team-a@example.com"),
    }


@pytest.fixture
def stub_bq_for_endpoint(monkeypatch):
    """Mirrors `tests/test_query_remote_rewrite.py`'s fixture of the same
    name -- stubs the dry-run estimate + BQ project resolution so the
    request reaches the push-down-eligibility decision (`did_rewrite`)
    without issuing real RPCs."""
    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", lambda *a, **k: 1024, raising=False)

    class _FakeProjects:
        data = "test-data-prj"
        billing = "test-billing-prj"

    class _FakeBqAccess:
        projects = _FakeProjects()

    monkeypatch.setattr("app.api.query.get_bq_access", lambda: _FakeBqAccess(), raising=False)


class _RecordingStubAnalytics:
    """Stands in for `get_analytics_db_readonly()`.

    A non-admin caller's request legitimately makes a FEW analytics calls
    that have nothing to do with policy enforcement -- `_enforce_non_admin_sql_rbac`
    enumerates catalog views (`.fetchall()`) and attached catalogs
    (`.fetchone()`/`.fetchall()`) before this module ever reaches the
    policy branch. Those get an empty, harmless answer so that PRE-EXISTING
    RBAC bookkeeping doesn't itself crash the request with an unrelated
    error that would mask this test's actual signal.

    `fetchmany_calls` is the narrow, precise thing this test suite cares
    about: the SQL text of every call whose result was actually FETCHED
    via `.fetchmany()` -- the one method both the ATTACH-catalog execution
    path and the (removed, for policied queries) push-down retry use to
    hand back analyst-visible rows. If that list is empty, no execution
    path handed back rows for this request at all; if it is NOT empty
    despite a `run_bq_query_to_arrow` failure, the fallback fired and
    would have leaked whatever canned rows are returned below.
    """

    description = [("id",)]

    def __init__(self):
        self.fetchmany_calls: list[str] = []

    def execute(self, sql, *args, **kwargs):
        outer = self

        class _Result:
            def fetchall(self):
                return []

            def fetchone(self):
                return None

            def fetchmany(self, _n):
                outer.fetchmany_calls.append(sql)
                return [("unfiltered-row-1",), ("unfiltered-row-2",), ("unfiltered-row-3",)]

        return _Result()

    def close(self):
        pass


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestFailClosedOnBigQueryExecutionFailure:
    """(c) A policied query_mode='remote' table whose BQ execution raises
    must deny, never degrade to the unfiltered original (§7.4, §17)."""

    def test_bq_failure_returns_an_error_with_no_rows_and_never_touches_analytics(
        self, policied_bq_invoices, stub_bq_for_endpoint, monkeypatch
    ):
        stub_analytics = _RecordingStubAnalytics()
        monkeypatch.setattr("app.api.query.get_analytics_db_readonly", lambda: stub_analytics, raising=False)

        def _raise(*a, **k):
            raise RuntimeError("BigQuery: access denied for policy-substituted query")

        monkeypatch.setattr("app.api.query.run_bq_query_to_arrow", _raise, raising=False)

        c = policied_bq_invoices["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM invoices"},
            headers=_auth(policied_bq_invoices["team_a_token"]),
        )

        assert r.status_code != 200, r.text
        assert r.status_code == 500, r.text
        body = r.json()
        assert body["detail"]["reason"] == "policy_error"
        assert body["detail"]["table"] == "bq.fin.invoices"
        assert "rows" not in body, "an error response must never carry a rows key"
        # The property this whole task exists to guarantee: the removed
        # fallback never fires. Not with request.sql, not with any
        # rewritten form of it -- no execution path handed rows back for
        # this request at all.
        assert stub_analytics.fetchmany_calls == [], (
            f"a row-returning execute() fired with {stub_analytics.fetchmany_calls!r} -- "
            "a policied remote-table query must never fall back to a "
            "DuckDB-side execution when its BigQuery job fails"
        )

    def test_original_unfiltered_sql_never_reaches_a_successful_execution(
        self, policied_bq_invoices, stub_bq_for_endpoint, monkeypatch
    ):
        """Same scenario, phrased as the task's own fail-closed check:
        `execute(request.sql)` -- or anything else -- is never called for
        the policied case, so it can never SUCCEED and leak rows either."""
        stub_analytics = _RecordingStubAnalytics()
        monkeypatch.setattr("app.api.query.get_analytics_db_readonly", lambda: stub_analytics, raising=False)
        monkeypatch.setattr(
            "app.api.query.run_bq_query_to_arrow",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bq down")),
            raising=False,
        )

        c = policied_bq_invoices["client"]
        original_sql = "SELECT * FROM invoices"
        r = c.post("/api/query", json={"sql": original_sql}, headers=_auth(policied_bq_invoices["team_a_token"]))

        assert r.status_code == 500, r.text
        assert original_sql not in stub_analytics.fetchmany_calls
        assert stub_analytics.fetchmany_calls == []
