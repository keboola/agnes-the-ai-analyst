"""POST /api/query cost guardrail for query_mode='remote' BigQuery rows.

When user SQL references a registered remote-BQ name (or a direct
`bq."<ds>"."<tbl>"` path), run a BQ dry-run before execute. If the
estimated scan exceeds the configured cap, reject with 400 +
`remote_scan_too_large` so the operator pivots to `agnes snapshot create`.

Default cap: 5 GiB per request. Configurable via
`api.query.bq_max_scan_bytes` in /admin/server-config (#160 §4.4).
"""
from __future__ import annotations

import pytest


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
def mock_dry_run(monkeypatch):
    """Replace `_bq_dry_run_bytes` with a controllable stub. Each test sets
    `mock_dry_run["bytes"]` to control what /api/query sees. Also stubs
    `get_bq_access` so the guardrail doesn't require a real BQ connection
    in the test env."""
    state = {"bytes": 0}

    def fake_dry_run(*args, **kwargs):
        return state["bytes"]

    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", fake_dry_run, raising=False)

    # Stub get_bq_access so the guardrail's BqAccess construction doesn't
    # fail with `not_configured` in tests that don't set up real BQ.
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
    return state


def test_query_under_cap_calls_dry_run(seeded_app, mock_dry_run, monkeypatch):
    """Dry-run is invoked when SQL references a registered remote BQ row.
    Use a sentinel side-effect to confirm: the mock records call counts."""
    _register_bq_remote_row("ue", "finance", "ue")
    state = mock_dry_run
    state["bytes"] = 1 * 1024 * 1024  # 1 MiB
    state["call_count"] = 0

    def counting_fake(*args, **kwargs):
        state["call_count"] += 1
        return state["bytes"]

    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", counting_fake, raising=False)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.post(
        "/api/query",
        json={"sql": "SELECT count(*) FROM ue"},
        headers=_auth(token),
    )
    assert state["call_count"] >= 1, \
        "guardrail must invoke _bq_dry_run_bytes when SQL references a registered remote BQ row"


def test_query_over_cap_rejected_400(seeded_app, mock_dry_run, monkeypatch):
    """Dry-run reports 10 GiB; default cap (5 GiB) is exceeded → 400 with
    structured detail naming bytes + tables + suggestion."""
    _register_bq_remote_row("ue", "finance", "ue")
    mock_dry_run["bytes"] = 10 * 1024 * 1024 * 1024  # 10 GiB

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM ue"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("reason") == "remote_scan_too_large", detail
        assert detail.get("scan_bytes") >= 10 * 1024 * 1024 * 1024
        # Suggestion text was renamed in the agnes-bootstrap PR (`da fetch`
        # → `agnes snapshot create`). Accept the new shape.
        suggestion = detail.get("suggestion", "").lower()
        assert "agnes snapshot create" in suggestion or "snapshot create" in suggestion
        assert "ue" in detail.get("tables", []) or \
               any("ue" in t for t in detail.get("tables", []))


def test_no_bq_row_reference_skips_dry_run(seeded_app, monkeypatch):
    """A query that doesn't touch any registered BQ remote row must NOT
    invoke `_bq_dry_run_bytes` — guardrail incurs zero new latency on
    plain non-BQ queries."""
    state = {"calls": 0}

    def counting_fake(*args, **kwargs):
        state["calls"] += 1
        return 100 * 1024 * 1024 * 1024  # 100 GiB — irrelevant if not called

    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", counting_fake, raising=False)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.post(
        "/api/query",
        json={"sql": "SELECT 1 AS x"},
        headers=_auth(token),
    )
    assert state["calls"] == 0, \
        f"guardrail must skip dry-run on non-BQ queries; got {state['calls']} calls"


# ---------------------------------------------------------------------------
# Issue #171: pre-check used to dry-run synthetic SELECT * per registered
# table → 30,000× over-estimate on partitioned/clustered tables. Fix: rewrite
# user SQL from DuckDB-flavor (bare names + `bq.<ds>.<tbl>`) to BQ-native
# (\\`<project>.<ds>.<tbl>\\`) and run a SINGLE dry-run on the user's actual
# SQL, so partition pruning, column projection, and predicate pushdown all
# count toward the cap check.
# ---------------------------------------------------------------------------


def test_guardrail_dry_runs_rewritten_user_sql_not_synthetic_select_star(
    seeded_app, mock_dry_run, monkeypatch,
):
    """The dry-run must receive the USER's SQL with bare table names rewritten
    to backticked paths — not a synthetic ``SELECT * FROM <table>``.

    This is the load-bearing assertion for issue #171: if the pre-check sees
    only the table name it can't prune partitions or project columns, and
    the estimate balloons to "full table size" instead of "what BQ would
    actually scan."
    """
    _register_bq_remote_row("ue", "finance", "ue")
    captured = {"sql": None}

    def capturing_fake(_bq, sql):
        captured["sql"] = sql
        return 1024  # tiny — pass the cap

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", capturing_fake, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    user_sql = (
        "SELECT order_id FROM ue "
        "WHERE event_date = DATE '2026-04-30' AND country = 'CZ'"
    )
    c.post("/api/query", json={"sql": user_sql}, headers=_auth(token))

    sent = captured["sql"]
    assert sent is not None, "dry-run never invoked"
    # User-side filters must survive the rewrite — that's the whole point of
    # the fix, partition pruning + predicate pushdown only engage in the BQ
    # planner if the WHERE clause reaches it.
    assert "event_date" in sent, f"WHERE clause stripped from dry-run SQL: {sent!r}"
    assert "country" in sent, f"WHERE clause stripped from dry-run SQL: {sent!r}"
    # Bare name `ue` must have been rewritten to a backticked
    # `<project>.finance.ue` path (project comes from the test stub
    # `_FakeProjects.data = "test-data-prj"`).
    assert "`test-data-prj.finance.ue`" in sent, (
        f"bare-name rewrite failed; sent SQL: {sent!r}"
    )
    # Pre-#171 path emitted `SELECT * FROM`; the new path forwards the
    # user SELECT clause untouched.
    assert "SELECT order_id" in sent, (
        f"pre-check is still using synthetic SELECT *; sent SQL: {sent!r}"
    )


def test_guardrail_invokes_dry_run_exactly_once_per_request(
    seeded_app, mock_dry_run, monkeypatch,
):
    """Single dry-run path: even when the user references multiple registered
    tables in one query (a JOIN, a UNION, …), only ONE dry-run fires.

    Pre-#171 the pre-check ran N dry-runs (one synthetic SELECT * per table)
    and summed. Now BQ does the joining for us in a single dry-run — cheaper
    AND more accurate (joins/filters/projections apply across both sides).
    """
    _register_bq_remote_row("orders", "finance", "orders")
    _register_bq_remote_row("traffic", "marketing", "traffic")

    state = {"call_count": 0, "last_sql": None}

    def counting_fake(_bq, sql):
        state["call_count"] += 1
        state["last_sql"] = sql
        return 100  # tiny

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", counting_fake, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.post(
        "/api/query",
        json={
            "sql": (
                "SELECT o.id, t.views FROM orders o "
                "JOIN traffic t ON o.date = t.date"
            ),
        },
        headers=_auth(token),
    )
    assert state["call_count"] == 1, (
        f"single-dry-run path expected; got {state['call_count']} calls"
    )
    # Both bare names rewritten in the same SQL.
    assert "`test-data-prj.finance.orders`" in state["last_sql"]
    assert "`test-data-prj.marketing.traffic`" in state["last_sql"]


def test_guardrail_falls_back_to_per_table_estimate_on_bq_parse_error(
    seeded_app, mock_dry_run, monkeypatch,
):
    """When BQ rejects the rewritten SQL with ``bq_bad_request`` (DuckDB-only
    syntax that doesn't translate — e.g. ``::INT`` casts, ``STRPOS``, …),
    the cap-guard falls back to the pre-#171 per-table SELECT * approach
    so a non-portable query still gets a (loose) cap estimate instead of
    fail-opening.
    """
    from connectors.bigquery.access import BqAccessError

    _register_bq_remote_row("ue", "finance", "ue")

    state = {"calls": []}

    def fake_dry_run(_bq, sql):
        state["calls"].append(sql)
        # First call (rewritten user SQL) → BQ parse error.
        if len(state["calls"]) == 1:
            raise BqAccessError("bq_bad_request", "Syntax error: unexpected '::'")
        # Second call (fallback per-table SELECT *) → small bytes, pass cap.
        return 4096

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", fake_dry_run, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    # SQL with DuckDB-only `::INT` cast that BQ would reject.
    r = c.post(
        "/api/query",
        json={"sql": "SELECT order_id::INT FROM ue WHERE country = 'CZ'"},
        headers=_auth(token),
    )

    # Two dry-runs (rewritten + fallback per-table) before the (failed)
    # execute. Status will be a downstream error from analytics.execute()
    # since `::INT` doesn't work in DuckDB either against a remote view —
    # but the GUARDRAIL must have completed without 5xx-ing.
    assert len(state["calls"]) == 2, (
        f"expected 1 rewritten + 1 fallback dry-run, got {len(state['calls'])}: "
        f"{state['calls']}"
    )
    assert "::" in state["calls"][0], "first call should be the rewritten user SQL"
    assert state["calls"][1].startswith("SELECT * FROM"), (
        "second call should be the per-table fallback"
    )
    # Whatever HTTP status comes back must NOT be 502 from the guard's
    # transport-error path — fallback must absorb the bq_bad_request.
    assert r.status_code != 502, r.json()


def test_guardrail_propagates_502_on_non_parse_bq_errors(
    seeded_app, mock_dry_run, monkeypatch,
):
    """Forbidden / upstream-error from BQ on the dry-run still maps to 502;
    fallback only kicks in for parse errors. Important so a misconfigured
    SA doesn't silently fall back to a stale-metadata estimate."""
    from connectors.bigquery.access import BqAccessError

    _register_bq_remote_row("ue", "finance", "ue")

    def always_forbidden(_bq, _sql):
        raise BqAccessError("bq_forbidden", "Permission denied", details={})

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", always_forbidden, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT count(*) FROM ue"},
        headers=_auth(token),
    )
    assert r.status_code == 502, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("kind") == "bq_forbidden"


def test_rewrite_helper_handles_bare_name_and_bq_path_in_same_sql():
    """Direct unit-test of the rewriter so the exact regex behavior is
    pinned: both bare names AND ``bq.<ds>.<tbl>`` references in the same
    SQL are translated, and longer names win over shorter prefixes.
    """
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql=(
            'SELECT a.id, b.col '
            'FROM ue a JOIN bq."finance"."traffic" b ON a.date = b.date'
        ),
        name_lookups=[("ue", "finance", "ue")],
        project="data-prj",
    )
    assert "`data-prj.finance.ue`" in rewritten
    assert "`data-prj.finance.traffic`" in rewritten
    # Original duckdb-flavor `bq."ds"."t"` form should have been replaced —
    # if it's still in the output, the BQ.path pass missed it.
    assert 'bq."finance"."traffic"' not in rewritten


def test_rewrite_helper_longer_name_wins_over_prefix():
    """When two registered names share a prefix (`unit_economics`,
    `unit_economics_summary`), the longer one must rewrite first so the
    shorter one's regex doesn't eat the prefix and leave junk like
    ``\\`...ue\\`_summary`` behind.
    """
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql="SELECT * FROM unit_economics_summary",
        name_lookups=[
            ("unit_economics", "fin", "ue"),
            ("unit_economics_summary", "fin", "ue_summary"),
        ],
        project="p",
    )
    assert "`p.fin.ue_summary`" in rewritten
    # If the shorter name had eaten the prefix we'd see `p.fin.ue`_summary
    # (broken token). Assert that doesn't happen.
    assert "`p.fin.ue`" not in rewritten


def test_rewrite_helper_does_not_corrupt_when_project_id_contains_registered_name():
    """Regression for Devin Review on query.py:464.

    Pre-fix the rewriter ran one `re.sub(\\bname\\b, ...)` per registered
    table, longest-first. When the GCP project ID contained a registered
    table name as a hyphen-delimited word (e.g. project=`my-ue-project`,
    registered name=`ue`), iter N's `\\b` regex would match INSIDE the
    backticked replacement text from a PRIOR iter, corrupting the output.

    Concrete trace:
    - SQL: ``FROM orders JOIN ue ON ...``
    - Iter 1 (orders): produces ``FROM `my-ue-project.fin.orders` JOIN ue ON``
    - Iter 2 (ue): `\\bue\\b` matches `ue` inside `my-ue-project` (hyphen =
      word boundary on both sides) → corrupts the iter-1 path.

    Post-fix: single `re.sub` with an alternation regex processes each
    source position exactly once. Freshly-inserted backticked text is
    NOT re-scanned by subsequent name patterns.
    """
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql="SELECT * FROM orders JOIN ue ON orders.id = ue.id",
        name_lookups=[
            ("orders", "fin", "orders"),
            ("ue", "analytics", "ue_metrics"),
        ],
        project="my-ue-project",
    )

    # Both names rewritten exactly once. Critically, the orders path is
    # NOT corrupted by a stray rewrite of `ue` inside `my-ue-project`.
    assert "`my-ue-project.fin.orders`" in rewritten
    assert "`my-ue-project.analytics.ue_metrics`" in rewritten

    # The corruption signature: the orders path would contain a nested
    # backtick-fenced ue path. Pinning this absence is the load-bearing
    # assertion — it fails on the pre-fix iterative rewriter.
    assert "`my-`my-ue-project.analytics.ue_metrics`-project" not in rewritten

    # Bare `ue` outside backticks (the JOIN clause) should be rewritten.
    # The 2nd `ue.id` was already rewritten by the same single-pass call.
    # No `\\bue\\b` survives outside backticks.
    import re as _re
    bare_ue_matches = _re.findall(r"(?<!\\.)\\bue\\b(?![.`])", rewritten)
    assert not bare_ue_matches, f"unrewritten bare `ue` left: {bare_ue_matches!r}"


def test_rewrite_helper_is_case_insensitive_on_bare_names():
    """Bare-name match in `_bq_guardrail_inputs` is case-insensitive (it
    runs against `sql_lower`). The rewriter must match the same set of
    occurrences on the original-case SQL or we'd silently leave some
    references untranslated and dry-run on a half-rewritten SQL.
    """
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql="SELECT * FROM UE WHERE Ue.id IS NOT NULL",
        name_lookups=[("ue", "fin", "ue")],
        project="p",
    )
    assert "`p.fin.ue` WHERE `p.fin.ue`.id" in rewritten or \
           rewritten.lower().count("`p.fin.ue`") == 2
