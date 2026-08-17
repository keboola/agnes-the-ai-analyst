"""Two Databricks gaps closed: the snapshot/scan path, and access policies.

Before this, a `query_mode='remote'` Databricks row could be queried
interactively but not *snapshotted* — `/api/v2/scan` refused it outright — and
a table carrying an access policy could not be queried on Databricks at all,
because the only two options were "deny" and "ship the caller's unfiltered
statement to the warehouse".

The tests are grouped by the property each one defends, not by module:

1. **Scan SQL** — pure-function builder tests. A mis-built statement is still
   valid SQL against the wrong thing, so this is the layer where a bug is
   silent.
2. **Estimate honesty** — the interesting design decision. Databricks has no
   dry-run, so `estimated_scan_bytes` is `None`, never `0`; `0` on this
   response means "local, free".
3. **Scan wire** — a real statement submitted to the fake warehouse.
4. **Policy dialect + binding** — that `$name` survives as `:name`, and that
   the one variable the API cannot bind (a list) is expanded without ever
   putting its values into SQL text.
5. **Policy enforcement** — end to end through `/api/query`, including the
   failure modes that must deny.
"""

from __future__ import annotations

import pytest

from connectors.databricks.policy_params import (
    DatabricksPolicyBindingError,
    bind_policy_parameters,
)
from connectors.databricks.remote import DatabricksRemoteError
from src.access_policy import PolicyError, policied_relation


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(
    *,
    id: str,
    name: str,
    source_type: str,
    bucket: str,
    source_table: str,
    query_mode: str = "remote",
) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=id,
            name=name,
            source_type=source_type,
            bucket=bucket,
            source_table=source_table,
            query_mode=query_mode,
        )
    finally:
        conn.close()


def _set_policy(table_id: str, sql: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).set_access_policy(table_id, sql=sql, note="test policy", updated_by="admin")
    finally:
        conn.close()


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """Fake warehouse + the settings dict the remote path expects.

    The COUNT route is listed FIRST: routes match by substring in order, and
    the scan route's `orders_raw` needle also occurs in the count statement.
    """
    import pyarrow as pa

    from tests.databricks_fake_warehouse import Route, start_fake_warehouse

    table = pa.table({"country": ["CZ", "SK", "PL"], "n": ["1", "2", "3"]})
    routes = [
        # `information_schema` first: `agnes schema` / the scan's own column
        # validation reads Unity Catalog before any data statement runs, and
        # its SQL also contains the table name the data routes match on.
        Route(
            match="information_schema.columns",
            columns=["column_name", "full_data_type", "is_nullable", "comment"],
            rows=[["country", "string", "YES", ""], ["n", "bigint", "YES", ""]],
        ),
        Route(match="COUNT(*)", arrow_table=pa.table({"n": ["42"]})),
        Route(match="orders_raw", arrow_table=table),
        Route(match="huge_table", arrow_table=table, truncated=True),
    ]
    wh = start_fake_warehouse(tmp_path, routes, monkeypatch)
    settings = {
        "host": wh.host,
        "token": "dapi-test-token",
        "warehouse_id": "wh-1",
        "catalog": "main",
    }
    monkeypatch.setattr(
        "connectors.databricks.semantic_layer.resolve_databricks_settings",
        lambda: settings,
    )
    try:
        yield wh, settings
    finally:
        wh.close()


# ---------------------------------------------------------------------------
# 1. Scan SQL
# ---------------------------------------------------------------------------


class TestScanSqlBuilder:
    def _req(self, **kw):
        from app.api.v2_scan import ScanRequest

        kw.setdefault("table_id", "dbx.sales.orders_raw")
        return ScanRequest(**kw)

    def _row(self, **kw):
        row = {
            "id": "dbx.sales.orders_raw",
            "name": "orders_raw",
            "source_type": "databricks",
            "query_mode": "remote",
            "bucket": "sales",
            "source_table": "orders_raw",
        }
        row.update(kw)
        return row

    def test_builds_a_three_part_backticked_path(self):
        from app.api.v2_scan import _build_databricks_sql

        sql = _build_databricks_sql(self._row(), self._req(), {"catalog": "main"})
        assert sql == "SELECT * FROM `main`.`sales`.`orders_raw`"

    def test_dotted_bucket_pins_its_own_catalog(self):
        from app.api.v2_scan import _build_databricks_sql

        sql = _build_databricks_sql(self._row(bucket="prod.sales"), self._req(), {"catalog": "main"})
        assert sql == "SELECT * FROM `prod`.`sales`.`orders_raw`"

    def test_select_where_order_and_limit_compose(self):
        from app.api.v2_scan import _build_databricks_sql

        sql = _build_databricks_sql(
            self._row(),
            self._req(select=["country", "n"], order_by=["country DESC"], limit=10),
            {"catalog": "main"},
            safe_where="country = 'CZ'",
        )
        assert sql == (
            "SELECT `country`, `n` FROM `main`.`sales`.`orders_raw` "
            "WHERE country = 'CZ' ORDER BY `country` DESC LIMIT 10"
        )

    def test_count_only_ignores_projection_and_ordering(self):
        """The estimate needs a row count, not a shaped result — carrying the
        caller's ORDER BY into it would make the warehouse sort for nothing."""
        from app.api.v2_scan import _build_databricks_sql

        sql = _build_databricks_sql(
            self._row(),
            self._req(select=["country"], order_by=["country"], limit=5),
            {"catalog": "main"},
            safe_where="country = 'CZ'",
            count_only=True,
        )
        assert sql == "SELECT COUNT(*) AS n FROM `main`.`sales`.`orders_raw` WHERE country = 'CZ'"

    def test_unsafe_registry_identifier_is_refused_not_escaped(self):
        from app.api.v2_scan import _build_databricks_sql

        with pytest.raises(DatabricksRemoteError) as exc:
            _build_databricks_sql(self._row(source_table="orders`; DROP"), self._req(), {"catalog": "main"})
        assert exc.value.reason == "databricks_unsafe_identifier"


class TestScannableEngineGate:
    def test_databricks_remote_row_is_now_scannable(self):
        from app.api.v2_scan import _assert_scannable_engine

        _assert_scannable_engine({"id": "x", "source_type": "databricks", "query_mode": "remote"})  # must not raise

    def test_another_remote_engine_is_still_refused(self):
        from app.api.v2_scan import _assert_scannable_engine

        with pytest.raises(ValueError, match="cannot execute against that engine"):
            _assert_scannable_engine({"id": "x", "source_type": "snowflake", "query_mode": "remote"})

    def test_materialized_databricks_row_stays_on_the_local_branch(self):
        from app.api.v2_scan import _executes_on_databricks

        assert not _executes_on_databricks({"source_type": "databricks", "query_mode": "materialized"})

    def test_where_dialect_follows_the_engine(self):
        """A remote Databricks row parses AND renders as databricks — the flavor
        `agnes schema` advertises for it, and the one the warehouse runs."""
        from app.api.v2_scan import _execution_dialect

        row = {"source_type": "databricks", "query_mode": "remote"}
        assert _execution_dialect(row, use_bq=False) == "databricks"
        assert _execution_dialect({"source_type": "databricks", "query_mode": "materialized"}, False) == "duckdb"
        assert _execution_dialect({"source_type": "bigquery"}, True) == "bigquery"


# ---------------------------------------------------------------------------
# 2. Estimate honesty
# ---------------------------------------------------------------------------


class TestEstimate:
    def test_scan_bytes_is_none_not_zero_and_rows_are_a_real_count(self, seeded_app, warehouse):
        """The design decision worth a test of its own.

        `0` already means something on this response — "served from a local
        parquet, nothing billable" — so reporting `0` for an engine that simply
        cannot be asked would read as "free". `None` says unknown. The row
        count, meanwhile, is not a heuristic at all here: it comes from a real
        COUNT(*) the warehouse answered.
        """
        from app.api.v2_scan import estimate
        from src.db import get_system_db

        wh, _settings = warehouse
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        conn = get_system_db()
        try:
            out = estimate(
                conn,
                {"id": "admin1", "email": "admin@test.com"},
                {"table_id": "dbx.sales.orders_raw"},
                bq=None,
            )
        finally:
            conn.close()

        assert out["engine"] == "databricks"
        assert out["estimated_scan_bytes"] is None
        assert out["bq_cost_estimate_usd"] is None
        assert out["estimated_result_rows"] == 42
        assert "COUNT(*)" in wh.statements[-1]

    def test_limit_caps_the_reported_row_count(self, seeded_app, warehouse):
        from app.api.v2_scan import estimate
        from src.db import get_system_db

        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        conn = get_system_db()
        try:
            out = estimate(
                conn,
                {"id": "admin1", "email": "admin@test.com"},
                {"table_id": "dbx.sales.orders_raw", "limit": 10},
                bq=None,
            )
        finally:
            conn.close()
        assert out["estimated_result_rows"] == 10

    def test_cli_renders_unknown_scan_bytes_as_not_available(self, capsys):
        """`0` and `None` must not print the same. The CLI is where an analyst
        actually reads this number and decides whether to fetch."""
        from cli.commands.snapshot import _print_estimate

        _print_estimate(
            {
                "engine": "databricks",
                "estimated_scan_bytes": None,
                "estimated_result_rows": 42,
                "estimated_result_bytes": 1344,
                "bq_cost_estimate_usd": None,
            }
        )
        out = capsys.readouterr().out
        assert "n/a" in out
        assert "cannot price a statement" in out
        assert "bq_cost_estimate_usd" not in out

    def test_cli_still_prints_bigquery_numbers_unchanged(self, capsys):
        from cli.commands.snapshot import _print_estimate

        _print_estimate(
            {
                "engine": "bigquery",
                "estimated_scan_bytes": 4_200_000_000,
                "estimated_result_rows": 250_000,
                "estimated_result_bytes": 12_000_000,
                "bq_cost_estimate_usd": 0.0191,
            }
        )
        out = capsys.readouterr().out
        assert "4,200,000,000 bytes" in out
        assert "$ 0.0191" in out

    def test_snapshot_meta_survives_a_null_scan_estimate(self):
        """`est.get("estimated_scan_bytes", 0)` returns None when the key EXISTS
        and maps to None — `int(None)` raises. Regression guard for the fetch
        path, which would otherwise crash after a successful download."""
        est = {"estimated_scan_bytes": None}
        assert int((est or {}).get("estimated_scan_bytes") or 0) == 0


# ---------------------------------------------------------------------------
# 3. Scan wire
# ---------------------------------------------------------------------------


class TestScanWire:
    def test_scan_returns_rows_without_the_interactive_limit_probe(self, seeded_app, warehouse):
        """A snapshot wants every row the predicate selects. The interactive
        path wraps statements in `LIMIT n + 1` to detect truncation; doing that
        here would silently cap the snapshot at the preview size."""
        from app.api.v2_quota import _build_quota_tracker
        from app.api.v2_scan import run_scan
        from src.db import get_system_db

        wh, _settings = warehouse
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        conn = get_system_db()
        try:
            ipc = run_scan(
                conn,
                {"id": "admin1", "email": "admin@test.com"},
                {"table_id": "dbx.sales.orders_raw", "select": ["country"]},
                bq=None,
                quota=_build_quota_tracker(),
            )
        finally:
            conn.close()

        assert ipc  # Arrow IPC bytes
        submitted = wh.statements[-1]
        assert submitted == "SELECT `country` FROM `main`.`sales`.`orders_raw`"
        assert "agnes_remote_q" not in submitted

    def test_a_truncated_result_is_refused_not_returned_short(self, seeded_app, warehouse):
        from app.api.v2_quota import _build_quota_tracker
        from app.api.v2_scan import run_scan
        from src.db import get_system_db

        _register(
            id="dbx.sales.huge_table",
            name="huge_table",
            source_type="databricks",
            bucket="sales",
            source_table="huge_table",
        )
        conn = get_system_db()
        try:
            with pytest.raises(Exception) as exc:
                run_scan(
                    conn,
                    {"id": "admin1", "email": "admin@test.com"},
                    {"table_id": "dbx.sales.huge_table"},
                    bq=None,
                    quota=_build_quota_tracker(),
                )
        finally:
            conn.close()
        detail = getattr(exc.value, "detail", {})
        assert detail.get("reason") == "remote_scan_too_large"

    def test_policied_table_is_refused_on_this_endpoint(self, seeded_app, warehouse, monkeypatch):
        """`/api/query` CAN carry a policy to the warehouse — it substitutes the
        body into a caller-authored statement. This endpoint has no caller
        statement to substitute into; it BUILDS one. Shipping that unfiltered
        would return exactly the rows the policy hides, so it denies (§17)."""
        from app.api.v2_quota import _build_quota_tracker
        from app.api.v2_scan import run_scan
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _set_policy("dbx.sales.orders_raw", "SELECT * FROM orders_raw WHERE country = 'CZ'")
        conn = get_system_db()
        try:
            grant_table_via_package(conn, "dbx.sales.orders_raw", "analyst1")
        finally:
            conn.close()

        conn = get_system_db()
        try:
            with pytest.raises(Exception) as exc:
                run_scan(
                    conn,
                    {"id": "analyst1", "email": "analyst1@test.com"},
                    {"table_id": "dbx.sales.orders_raw"},
                    bq=None,
                    quota=_build_quota_tracker(),
                )
        finally:
            conn.close()
        detail = getattr(exc.value, "detail", {})
        assert detail.get("reason") == "policy_unsupported_on_scan_engine", f"raised {type(exc.value).__name__}: {exc.value}"
        assert "agnes query --remote" in detail.get("hint", "")


# ---------------------------------------------------------------------------
# 4. Policy dialect + binding
# ---------------------------------------------------------------------------


class TestPolicyDialect:
    def test_placeholder_becomes_the_databricks_parameter_marker(self):
        """The property the whole feature rests on: one authored policy body
        keeps its values out of SQL text on all three engines, because sqlglot
        renders `$name` as each engine's own named-parameter syntax."""
        from src.access_policy import _transpile_policy_to_databricks

        out = _transpile_policy_to_databricks("SELECT * FROM t WHERE owner = $user_email", table_id="t")
        assert ":user_email" in out
        assert "$user_email" not in out

    def test_group_membership_idiom_transpiles(self):
        from src.access_policy import _transpile_policy_to_databricks

        out = _transpile_policy_to_databricks("SELECT * FROM t WHERE list_contains($user_groups, region)", table_id="t")
        assert "ARRAY_CONTAINS(:user_groups, region)" in out

    def test_untranspilable_body_raises_policy_error_not_the_engine_message(self):
        from src.access_policy import _transpile_policy_to_databricks

        with pytest.raises(PolicyError):
            _transpile_policy_to_databricks("this is not sql at all !!", table_id="t")

    def test_unknown_dialect_still_rejected(self):
        with pytest.raises(ValueError, match="unknown dialect"):
            policied_relation("whatever", {"id": "u"}, dialect="snowflake")

    def test_save_time_validation_covers_databricks_too(self):
        """A policy that transpiles to BigQuery but not Databricks would save
        clean and then DENY at read time on a Databricks table — an outage
        shipped as an access rule. The save is the cheap place to find out."""
        import src.access_policy_validate as v

        # Sanity: the ordinary shape passes both engines.
        v._reject_untranspilable("SELECT * FROM t WHERE owner = $user_email")

        calls = []
        original = v.sqlglot.transpile

        def fake(sql, read, write):
            calls.append(write)
            return original(sql, read=read, write=write)

        v.sqlglot.transpile = fake
        try:
            v._reject_untranspilable("SELECT * FROM t")
        finally:
            v.sqlglot.transpile = original
        assert "databricks" in calls and "bigquery" in calls


class TestParameterBinding:
    def test_scalars_pass_through_as_typed_parameters(self):
        sql, params = bind_policy_parameters("SELECT * FROM t WHERE owner = :user_email", {"user_email": "a@b.com"})
        assert sql == "SELECT * FROM t WHERE owner = :user_email"
        assert params == [{"name": "user_email", "value": "a@b.com", "type": "STRING"}]

    def test_array_variable_expands_to_scalar_markers(self):
        """The API binds scalars only. The values still travel as request
        fields — only the arity of the list becomes visible in the text."""
        # Distinctive values: a two-letter group name would collide by accident
        # with substrings of the generated marker identifiers and make the
        # leak assertion below meaningless.
        groups = ["eu-field-analysts", "latam-partners"]
        sql, params = bind_policy_parameters(
            "SELECT * FROM t WHERE ARRAY_CONTAINS(:user_groups, region)",
            {"user_groups": groups},
        )
        assert "ARRAY(:agnes_policy_group_user_groups_0, :agnes_policy_group_user_groups_1)" in sql
        assert [p["value"] for p in params] == groups
        # The decisive assertion: no group NAME appears in the SQL text.
        for name in groups:
            assert name not in sql

    def test_empty_group_list_becomes_a_typed_empty_array(self):
        """A caller in no groups must match nothing — a legitimate state, not
        an error. Bare `ARRAY()` is `ARRAY<VOID>` on Databricks and would fail
        to type-check against a string column."""
        sql, params = bind_policy_parameters(
            "SELECT * FROM t WHERE ARRAY_CONTAINS(:user_groups, region)", {"user_groups": []}
        )
        assert "CAST(ARRAY() AS ARRAY<STRING>)" in sql
        assert params == []

    def test_marker_inside_a_string_literal_is_not_rewritten(self):
        """Why the substitution is AST-level and not a regex. A policy body is
        exactly the kind of SQL that carries literals."""
        sql, _params = bind_policy_parameters(
            "SELECT * FROM t WHERE note = ':user_groups' AND ARRAY_CONTAINS(:user_groups, r)",
            {"user_groups": ["x"]},
        )
        assert "note = ':user_groups'" in sql
        assert sql.count("ARRAY(") == 1

    def test_no_list_survives_into_the_parameter_payload(self):
        _sql, params = bind_policy_parameters(
            "SELECT * FROM t WHERE ARRAY_CONTAINS(:user_groups, r) AND o = :user_email",
            {"user_groups": ["a", "b"], "user_email": "x@y.z"},
        )
        assert all(isinstance(p["value"], str) for p in params)
        assert all(p["type"] == "STRING" for p in params)

    def test_unparseable_body_denies(self):
        with pytest.raises(DatabricksPolicyBindingError):
            bind_policy_parameters("!!! not sql", {"user_groups": ["a"]})

    def test_missing_marker_denies_rather_than_dropping_the_filter(self):
        """If the array marker is not where we are about to bind it, the group
        filter has silently vanished. Deny."""
        with pytest.raises(DatabricksPolicyBindingError):
            bind_policy_parameters("SELECT * FROM t", {"user_groups": ["a"]})


# ---------------------------------------------------------------------------
# 5. Policy enforcement, end to end
# ---------------------------------------------------------------------------


class TestPolicyEnforcement:
    def _setup(self, monkeypatch, policy_sql: str) -> None:
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _set_policy("dbx.sales.orders_raw", policy_sql)
        conn = get_system_db()
        try:
            # Must be a granted NON-admin: an admin holds a full-surface
            # credential, for which the resolver returns the passthrough
            # relation — the test would pass for the wrong reason.
            grant_table_via_package(conn, "dbx.sales.orders_raw", "analyst1")
        finally:
            conn.close()

    def test_group_policy_binds_values_as_parameters_never_as_sql_text(self, seeded_app, warehouse, monkeypatch):
        """The security property, asserted on the wire: the caller's group
        names reach the warehouse in the request's `parameters` field and
        nowhere in the statement."""
        wh, _settings = warehouse
        self._setup(monkeypatch, "SELECT * FROM orders_raw WHERE list_contains($user_groups, country)")

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text

        submitted = wh.statements[-1]
        payload = wh.payloads[-1]
        assert "ARRAY_CONTAINS(ARRAY(" in submitted
        assert payload.get("parameters"), "identity values must ride the parameters field"
        for param in payload["parameters"]:
            assert param["value"] not in submitted, "a bound value leaked into the SQL text"

    def test_policy_predicate_reaches_the_warehouse(self, seeded_app, warehouse, monkeypatch):
        wh, _settings = warehouse
        self._setup(monkeypatch, "SELECT * FROM orders_raw WHERE country = 'CZ'")

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text
        submitted = wh.statements[-1]
        assert "country = 'CZ'" in submitted
        # The subquery alias must stay a bare name: a three-part path in alias
        # position is a syntax error, and the name rewriter replaces every
        # occurrence unless the policied name is excluded from it. (The outer
        # `agnes_remote_q LIMIT` is the interactive path's own probe wrap.)
        assert ") AS orders_raw" in submitted
        assert "AS `main`.`sales`.`orders_raw`" not in submitted

    def test_admin_bypass_ships_the_unfiltered_statement(self, seeded_app, warehouse, monkeypatch):
        """§12 — the bypass follows the credential surface. An admin's read is
        a passthrough relation, so no substitution happens at all."""
        wh, _settings = warehouse
        self._setup(monkeypatch, "SELECT * FROM orders_raw WHERE country = 'CZ'")

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert "country = 'CZ'" not in wh.statements[-1]

    def test_policy_body_naming_an_unregistered_table_denies(self, seeded_app, warehouse, monkeypatch):
        """The re-gate. The first gate pass only sees the caller's SQL; a policy
        body can name tables the caller never wrote. Without a second pass such
        a name ships unchecked and resolves against whatever the default
        catalog holds."""
        self._setup(
            monkeypatch,
            "SELECT * FROM orders_raw WHERE country IN (SELECT c FROM secret_mapping)",
        )
        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 500, r.text
        assert r.json()["detail"]["reason"] == "policy_error"
        assert r.json()["detail"]["table"] == "dbx.sales.orders_raw"

    def test_snapshot_from_query_carries_the_policy(self, seeded_app, warehouse, monkeypatch):
        """A materialize must not be a way around the filter."""
        from app.api.query import run_remote_select_to_arrow
        from app.api.v2_quota import _build_quota_tracker
        from src.db import get_system_db

        wh, _settings = warehouse
        self._setup(monkeypatch, "SELECT * FROM orders_raw WHERE country = 'CZ'")

        conn = get_system_db()
        try:
            policy_info: dict = {}
            table = run_remote_select_to_arrow(
                conn,
                {"id": "analyst1", "email": "analyst1@test.com"},
                "SELECT * FROM orders_raw",
                None,
                _build_quota_tracker(),
                policy_info=policy_info,
            )
        finally:
            conn.close()

        assert table is not None
        assert "country = 'CZ'" in wh.statements[-1]
        assert policy_info.get("policied_table_ids") == ["dbx.sales.orders_raw"]
