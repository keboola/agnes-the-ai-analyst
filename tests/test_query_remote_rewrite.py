"""Unit tests for ``_rewrite_user_sql_for_bigquery_query``.

The helper rewrites user SQL referencing query_mode='remote' BigQuery
tables so the entire query ships to BQ via the DuckDB BQ extension's
``bigquery_query(<project>, <sql>)`` UDF — engaging WHERE / SELECT /
LIMIT predicate pushdown instead of falling through to ATTACH-catalog
mode (which opens a Storage Read API session over the whole table).

These tests pin down each conservative-skip rule plus the happy-path
rewrites. Edge cases (CTE shadowing, double-wrap, mixed-source JOIN)
are intentionally explicit so a future refactor doesn't quietly
loosen the guard.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Test infrastructure: an in-memory DuckDB seeded with table_registry rows
# matching the shapes the production registry produces. Avoids the full app
# bootstrap path; the rewriter only needs ``conn.execute("SELECT * FROM
# table_registry ...")`` to resolve names.
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_registry(tmp_path, monkeypatch):
    """Build a fresh ``system.duckdb`` in tmp_path with the schema migrated.

    Returns the open connection so tests can pass it to the rewriter.
    Cleanup is automatic via tmp_path teardown — but we close the
    open singleton handle first so a different DATA_DIR in the next
    test doesn't see the previous tmp's lock.
    """
    from src.db import get_system_db, close_system_db

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    close_system_db()
    conn = get_system_db()
    yield conn
    close_system_db()


def _register_bq_remote(conn, *, table_id, name, bucket, source_table):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id=table_id,
        name=name,
        source_type="bigquery",
        bucket=bucket,
        source_table=source_table,
        query_mode="remote",
    )


def _register_local(conn, *, table_id, name, source_type="keboola"):
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id=table_id,
        name=name,
        source_type=source_type,
        bucket="bkt",
        source_table=name,
        query_mode="local",
    )


def _set_bq_project(monkeypatch, project="test-prj", billing=None):
    """Stub get_bq_access so the rewriter sees a real-looking project ID.

    `project` configures the data project (used in backtick paths).
    `billing` (when provided) configures a different billing project so
    cross-project deployments can be exercised; defaults to `project`
    for the single-project case."""
    from connectors.bigquery.access import BqAccess, BqProjects, get_bq_access
    bq = BqAccess(
        BqProjects(billing=billing or project, data=project),
        client_factory=lambda projects: object(),
    )
    monkeypatch.setattr(
        "app.api.query.get_bq_access",
        lambda: bq,
        raising=False,
    )
    get_bq_access.cache_clear()


# ---------------------------------------------------------------------------
# Happy-path rewrites
# ---------------------------------------------------------------------------


def test_simple_select_where_against_one_bq_table_rewrites(seeded_registry, monkeypatch):
    """Single-table SELECT-WHERE against a registered BQ remote row →
    full SQL wrapped in ``bigquery_query('project', '<rewritten>')``.
    The bare-name reference gets translated to BQ-native backtick form."""
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_bq_remote(seeded_registry, table_id="bq.fin.ue", name="ue",
                        bucket="fin", source_table="ue")
    _set_bq_project(monkeypatch, "test-prj")

    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        "SELECT count(*) FROM ue WHERE event_date = '2026-01-01'",
        seeded_registry,
    )

    assert did_rewrite is True
    # Outer wrap must be a single bigquery_query() FROM-source.
    assert "bigquery_query(" in rewritten
    assert "test-prj" in rewritten
    # Inner SQL: bare name rewritten to backticked BQ-native path.
    assert "`test-prj.fin.ue`" in rewritten
    # Inner SQL is dollar-quoted (`$bqq_inner$ ... $bqq_inner$`), so
    # single quotes inside the WHERE predicate remain literal — no
    # doubling, no backslash escaping. Verifies the safer embedding form
    # introduced after the code review caught naive single-quote-only
    # escape doubling missing DuckDB backslash sequences.
    assert "$bqq_inner$" in rewritten
    assert "event_date = '2026-01-01'" in rewritten


def test_direct_bq_path_rewrites(seeded_registry, monkeypatch):
    """User wrote the direct ``bq."ds"."tbl"`` form. The rewriter must
    still translate to BQ-native backtick form before wrapping."""
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_bq_remote(seeded_registry, table_id="bq.fin.ue", name="ue",
                        bucket="fin", source_table="ue")
    _set_bq_project(monkeypatch, "test-prj")

    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        'SELECT * FROM bq."fin"."ue" LIMIT 10',
        seeded_registry,
    )

    assert did_rewrite is True
    assert "bigquery_query(" in rewritten
    assert "`test-prj.fin.ue`" in rewritten
    # Original duckdb-flavor path must NOT remain (it'd parse-fail under BQ).
    assert 'bq."fin"."ue"' not in rewritten


def test_cte_referencing_bq_table_rewrites_inside_cte(seeded_registry, monkeypatch):
    """A WITH clause whose body references a BQ table must rewrite that
    inner reference; the wrapping happens at the top level so BQ sees a
    valid BQ-flavor CTE."""
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_bq_remote(seeded_registry, table_id="bq.fin.orders", name="orders",
                        bucket="fin", source_table="orders")
    _set_bq_project(monkeypatch, "test-prj")

    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        "WITH x AS (SELECT id FROM orders WHERE total > 0) SELECT count(*) FROM x",
        seeded_registry,
    )
    assert did_rewrite is True
    # Inner reference is rewritten.
    assert "`test-prj.fin.orders`" in rewritten
    # The whole thing is wrapped — bigquery_query is the outermost FROM.
    assert rewritten.lower().count("bigquery_query(") == 1


def test_subquery_referencing_bq_table_rewrites(seeded_registry, monkeypatch):
    """Subquery in FROM position — same handling as a CTE: rewrite the
    inner table reference, wrap the whole at the top."""
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_bq_remote(seeded_registry, table_id="bq.fin.ue", name="ue",
                        bucket="fin", source_table="ue")
    _set_bq_project(monkeypatch, "test-prj")

    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        "SELECT s.cnt FROM (SELECT count(*) AS cnt FROM ue) s",
        seeded_registry,
    )
    assert did_rewrite is True
    assert "`test-prj.fin.ue`" in rewritten
    assert rewritten.lower().count("bigquery_query(") == 1


def test_multiple_bq_tables_one_project_combine(seeded_registry, monkeypatch):
    """Two registered BQ tables in the same project → single
    ``bigquery_query()`` wraps the whole SQL with both refs rewritten
    inline. No separate parallel calls."""
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_bq_remote(seeded_registry, table_id="bq.fin.orders", name="orders",
                        bucket="fin", source_table="orders")
    _register_bq_remote(seeded_registry, table_id="bq.fin.users", name="users",
                        bucket="fin", source_table="users")
    _set_bq_project(monkeypatch, "test-prj")

    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        "SELECT u.id, count(o.id) "
        "FROM users u JOIN orders o ON u.id = o.user_id "
        "GROUP BY u.id",
        seeded_registry,
    )
    assert did_rewrite is True
    # Both rewritten.
    assert "`test-prj.fin.users`" in rewritten
    assert "`test-prj.fin.orders`" in rewritten
    # Single wrap.
    assert rewritten.lower().count("bigquery_query(") == 1


# ---------------------------------------------------------------------------
# Conservative-skip cases
# ---------------------------------------------------------------------------


def test_join_bq_to_local_skips_rewrite(seeded_registry, monkeypatch):
    """A JOIN between a BQ table and a local-mode (Keboola/Jira) table
    is a cross-source query — wrapping it in bigquery_query() would lose
    the local table. The rewriter must fall through to the ATTACH-catalog
    path (slow but correct).
    """
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_bq_remote(seeded_registry, table_id="bq.fin.ue", name="ue",
                        bucket="fin", source_table="ue")
    _register_local(seeded_registry, table_id="kbc.in.local_orders",
                    name="local_orders")
    _set_bq_project(monkeypatch, "test-prj")

    user_sql = (
        "SELECT u.id, lo.total "
        "FROM ue u JOIN local_orders lo ON u.id = lo.user_id"
    )
    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        user_sql, seeded_registry,
    )
    assert did_rewrite is False
    assert rewritten == user_sql  # untouched


def test_no_bq_tables_passes_through(seeded_registry, monkeypatch):
    """User SQL referencing only local-source tables → no rewrite,
    no log spam, original SQL returned."""
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_local(seeded_registry, table_id="kbc.in.orders", name="orders")
    _set_bq_project(monkeypatch, "test-prj")

    user_sql = "SELECT * FROM orders WHERE id = 1"
    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        user_sql, seeded_registry,
    )
    assert did_rewrite is False
    assert rewritten == user_sql


def test_already_contains_bigquery_query_passes_through(seeded_registry, monkeypatch):
    """User SQL already calls bigquery_query() — never double-wrap.

    Note: the /api/query endpoint blocks ``bigquery_query`` in user SQL
    via the keyword denylist, so this scenario can't reach the rewriter
    in production today. Defensive guard for callers from other paths.
    """
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_bq_remote(seeded_registry, table_id="bq.fin.ue", name="ue",
                        bucket="fin", source_table="ue")
    _set_bq_project(monkeypatch, "test-prj")

    user_sql = (
        "SELECT * FROM bigquery_query('test-prj', 'SELECT * FROM `test-prj.fin.ue`')"
    )
    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        user_sql, seeded_registry,
    )
    assert did_rewrite is False
    assert rewritten == user_sql


def test_unconfigured_bq_project_skips(seeded_registry, monkeypatch):
    """If get_bq_access() is the not-configured sentinel (data=''),
    don't rewrite — there's no project to fill into bigquery_query()."""
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_bq_remote(seeded_registry, table_id="bq.fin.ue", name="ue",
                        bucket="fin", source_table="ue")

    # Override to sentinel (empty data project).
    from connectors.bigquery.access import BqAccess, BqProjects, get_bq_access
    monkeypatch.setattr(
        "app.api.query.get_bq_access",
        lambda: BqAccess(BqProjects(billing="", data="")),
        raising=False,
    )
    get_bq_access.cache_clear()

    user_sql = "SELECT * FROM ue"
    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        user_sql, seeded_registry,
    )
    assert did_rewrite is False
    assert rewritten == user_sql


# ---------------------------------------------------------------------------
# Backwards-compat: dry-run helper still available + behaves the same
# ---------------------------------------------------------------------------


def test_existing_dry_run_helper_still_callable():
    """The original ``_rewrite_user_sql_for_bq_dry_run`` is now a thin
    wrapper around the shared core rewriter (Pass 1 + Pass 2). Callers
    that pass an explicit ``project`` argument keep working unchanged.
    """
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql="SELECT * FROM ue",
        name_lookups=[("ue", "fin", "ue")],
        project="some-prj",
    )
    assert "`some-prj.fin.ue`" in rewritten
    # The dry-run helper does NOT add a bigquery_query() wrapper; that's
    # only the new execution-path helper's job.
    assert "bigquery_query(" not in rewritten


# ---------------------------------------------------------------------------
# End-to-end: the /api/query handler must invoke the rewriter and execute
# the rewritten SQL (not the original) when there's a BQ-remote table.
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_bq_remote_row(name: str, bucket: str, source_table: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    sys_conn = get_system_db()
    try:
        TableRegistryRepository(sys_conn).register(
            id=f"bq.{bucket}.{source_table}",
            name=name,
            source_type="bigquery",
            bucket=bucket,
            source_table=source_table,
            query_mode="remote",
        )
    finally:
        sys_conn.close()


@pytest.fixture
def stub_bq_for_endpoint(monkeypatch):
    """Stub _bq_dry_run_bytes + get_bq_access at the endpoint level so the
    cap-guard sees a real-looking BQ project but doesn't issue real RPCs.
    """
    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes",
        lambda *a, **k: 1024,  # tiny — pass cap
        raising=False,
    )

    class _FakeProjects:
        data = "test-data-prj"
        billing = "test-billing-prj"

    class _FakeBqAccess:
        projects = _FakeProjects()

    monkeypatch.setattr(
        "app.api.query.get_bq_access",
        lambda: _FakeBqAccess(),
        raising=False,
    )


def test_endpoint_executes_rewritten_sql_against_analytics(
    seeded_app, stub_bq_for_endpoint, monkeypatch,
):
    """The /api/query handler must call ``analytics.execute(rewritten_sql)``
    — NOT the user's original SQL — when a BQ-remote table is referenced.
    Capture what reaches DuckDB and assert the bigquery_query() wrap is
    present.
    """
    _register_bq_remote_row("ue", "fin", "ue")

    # Capture analytics.execute calls. The handler does
    # `analytics = get_analytics_db_readonly(); analytics.execute(sql)`,
    # so we patch the connection factory to return a stub.
    captured = {"sql": None}

    class _StubAnalytics:
        description = [("c0",)]
        def execute(self, sql, *args, **kwargs):
            captured["sql"] = sql
            class _R:
                def fetchmany(self, _n):
                    return []
            return _R()
        def close(self):
            pass

    monkeypatch.setattr(
        "app.api.query.get_analytics_db_readonly",
        lambda: _StubAnalytics(),
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT count(*) FROM ue WHERE country = 'CZ'"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.json()
    sent = captured["sql"]
    assert sent is not None, "analytics.execute was never called"
    assert "bigquery_query(" in sent, (
        f"endpoint did not wrap user SQL in bigquery_query(); sent: {sent!r}"
    )
    assert "test-data-prj" in sent
    assert "`test-data-prj.fin.ue`" in sent


def test_endpoint_passes_original_sql_when_no_bq_table(
    seeded_app, stub_bq_for_endpoint, monkeypatch,
):
    """For queries that don't touch any BQ-remote registered name, the
    handler must pass the original SQL through unchanged — the
    ATTACH-catalog path handles local-source tables natively and any
    rewrite would be wasted work."""
    captured = {"sql": None}

    class _StubAnalytics:
        description = [("c0",)]
        def execute(self, sql, *args, **kwargs):
            captured["sql"] = sql
            class _R:
                def fetchmany(self, _n):
                    return []
            return _R()
        def close(self):
            pass

    monkeypatch.setattr(
        "app.api.query.get_analytics_db_readonly",
        lambda: _StubAnalytics(),
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    user_sql = "SELECT 1 AS x"
    r = c.post("/api/query", json={"sql": user_sql}, headers=_auth(token))
    assert r.status_code == 200, r.json()
    assert captured["sql"] == user_sql
    assert "bigquery_query(" not in captured["sql"]


def test_endpoint_wraps_rewritten_sql_with_outer_limit(
    seeded_app, stub_bq_for_endpoint, monkeypatch,
):
    """Memory-safety regression — when the rewriter fires, the handler
    MUST wrap the bigquery_query() call in an outer ``LIMIT N+1`` so a
    `SELECT *` against a billion-row remote table doesn't materialise the
    full result into the worker before fetchmany applies the cap.
    Code-review #2a fix.
    """
    _register_bq_remote_row("ue", "fin", "ue")

    captured = {"sql": None}

    class _StubAnalytics:
        description = [("c0",)]
        def execute(self, sql, *args, **kwargs):
            captured["sql"] = sql
            class _R:
                def fetchmany(self, _n):
                    return []
            return _R()
        def close(self):
            pass

    monkeypatch.setattr(
        "app.api.query.get_analytics_db_readonly",
        lambda: _StubAnalytics(),
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM ue", "limit": 100},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.json()
    sent = captured["sql"]
    # The bigquery_query() wrap is present, AND the whole thing is wrapped
    # again with an outer LIMIT that includes the user-requested cap +1
    # (the +1 is the existing truncation-detection pattern).
    assert "bigquery_query(" in sent
    assert "_bqq_outer" in sent
    assert "LIMIT 101" in sent  # request.limit (100) + 1


def test_endpoint_falls_back_to_original_sql_on_bq_parse_error(
    seeded_app, stub_bq_for_endpoint, monkeypatch,
):
    """When the rewritten ``bigquery_query()`` path fails with a parse-
    level error (e.g. user SQL contained DuckDB-only syntax that BQ
    can't parse), the handler MUST retry with the original SQL via the
    ATTACH-catalog path so the user request still succeeds. Code-review
    #4 fix.
    """
    _register_bq_remote_row("ue", "fin", "ue")

    calls = {"sqls": []}

    class _StubAnalytics:
        description = [("c0",)]
        def execute(self, sql, *args, **kwargs):
            calls["sqls"].append(sql)
            # First call (rewritten) raises a BQ-style parse error;
            # second call (original SQL fallback) returns rows.
            if "bigquery_query(" in sql:
                raise RuntimeError(
                    "BinderException: Query execution failed: "
                    "Syntax error: Unexpected token at [1:42]"
                )
            class _R:
                def fetchmany(self, _n):
                    return [(1,)]
            return _R()
        def close(self):
            pass

    monkeypatch.setattr(
        "app.api.query.get_analytics_db_readonly",
        lambda: _StubAnalytics(),
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        # DuckDB-only ::INT cast — survives identifier rewrite, BQ refuses.
        json={"sql": "SELECT (count(*))::INT FROM ue"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.json()
    # Two execute calls: 1) rewritten (raised) 2) fallback to original.
    assert len(calls["sqls"]) == 2
    assert "bigquery_query(" in calls["sqls"][0]
    assert calls["sqls"][1] == "SELECT (count(*))::INT FROM ue"


def test_rewriter_uses_billing_project_for_bigquery_query_first_arg(
    seeded_registry, monkeypatch,
):
    """Devin-review BUG #1: `bigquery_query()` first arg is the
    **billing** project (where BQ jobs are billed + executed), backtick
    paths use the **data** project. In cross-project deploys the SA
    has `serviceusage.services.use` only on the billing project, so
    using the data project as billing → 403 USER_PROJECT_DENIED.

    Match the existing convention in v2_scan / v2_sample / v2_schema /
    extractor.
    """
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_bq_remote(
        seeded_registry, table_id="bq.fin.ue", name="ue",
        bucket="fin", source_table="ue",
    )
    _set_bq_project(monkeypatch, project="data-prj", billing="billing-prj")

    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        "SELECT count(*) FROM ue",
        seeded_registry,
    )
    assert did_rewrite is True
    # First arg of bigquery_query must be the billing project.
    assert "bigquery_query('billing-prj'" in rewritten
    # Backtick path must use the data project.
    assert "`data-prj.fin.ue`" in rewritten
    # And the data project must NOT appear as the first arg.
    assert "bigquery_query('data-prj'" not in rewritten


def test_rewriter_skips_when_bq_row_bucket_contains_dot(
    seeded_registry, monkeypatch,
):
    """Devil's-advocate R1 finding #5: a BQ row whose `bucket` contains
    `.` suggests the operator encoded a project prefix in the bucket
    name. Wrapping under our single-project assumption could silently
    target the wrong project. Rewriter must skip in that case (fall
    through to ATTACH-catalog path which respects the operator's
    `_remote_attach` configuration).
    """
    from app.api.query import _rewrite_user_sql_for_bigquery_query
    _register_bq_remote(
        seeded_registry,
        table_id="bq.other-prj.dataset.ue",
        name="ue",
        # Project-qualified bucket — the multi-project red flag.
        bucket="other-prj.dataset",
        source_table="ue",
    )
    _set_bq_project(monkeypatch, "test-prj")

    rewritten, did_rewrite = _rewrite_user_sql_for_bigquery_query(
        "SELECT count(*) FROM ue",
        seeded_registry,
    )
    # Skip — original SQL returned, no rewrite.
    assert did_rewrite is False
    assert rewritten == "SELECT count(*) FROM ue"


def test_fallback_does_not_trigger_on_user_column_typo(
    seeded_app, stub_bq_for_endpoint, monkeypatch,
):
    """Devil's-advocate R1 finding #2: previously the fallback
    heuristic matched `Unrecognized name`, which BQ surfaces for both
    DuckDB-only-name AND user-column-typo cases. The user-typo case
    triggered re-running the original SQL through the slow ATTACH-
    catalog path (90+ s) → 2× latency tax on every typo.

    Post-fix: heuristic only matches `Syntax error`. A BQ-side
    `Unrecognized name: bad_col` should propagate as-is, NOT trigger
    a fallback retry.
    """
    _register_bq_remote_row("ue", "fin", "ue")

    calls = {"sqls": []}

    class _StubAnalytics:
        description = [("c0",)]
        def execute(self, sql, *args, **kwargs):
            calls["sqls"].append(sql)
            raise RuntimeError(
                "BinderException: Query execution failed: "
                "Unrecognized name: bad_col at [1:8]"
            )
        def close(self):
            pass

    monkeypatch.setattr(
        "app.api.query.get_analytics_db_readonly",
        lambda: _StubAnalytics(),
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT bad_col FROM ue"},
        headers=_auth(token),
    )
    # Error propagates; fallback NOT triggered (only one execute call).
    assert r.status_code in (400, 500, 502)
    assert len(calls["sqls"]) == 1, (
        "user column typo must NOT trigger fallback retry"
    )
    assert "bigquery_query(" in calls["sqls"][0]


def test_endpoint_does_not_fall_back_on_non_parse_errors(
    seeded_app, stub_bq_for_endpoint, monkeypatch,
):
    """Non-parse-error exceptions from the rewritten path (network,
    quota, forbidden, generic runtime) must propagate, NOT silently
    retry against the legacy path. Otherwise the legacy path would
    just fail again and the user sees a slow + double-failure.
    """
    _register_bq_remote_row("ue", "fin", "ue")

    calls = {"sqls": []}

    class _StubAnalytics:
        description = [("c0",)]
        def execute(self, sql, *args, **kwargs):
            calls["sqls"].append(sql)
            raise RuntimeError("Network unreachable: BQ endpoint timed out")
        def close(self):
            pass

    monkeypatch.setattr(
        "app.api.query.get_analytics_db_readonly",
        lambda: _StubAnalytics(),
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT count(*) FROM ue"},
        headers=_auth(token),
    )
    # Generic 400 from the handler's outer except — body will surface
    # the runtime error message; we just need to confirm no fallback.
    assert r.status_code in (400, 500, 502)
    assert len(calls["sqls"]) == 1, "must not retry on non-parse error"
