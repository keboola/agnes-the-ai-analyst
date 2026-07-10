"""Cross-engine contract tests for the audit repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong.

This is the test that proves the dual-write window in the parent plan
(Phase 2 step 3) can work without invisible behaviour deltas.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import duckdb
import pytest


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------

def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.audit import AuditRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return AuditRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.audit_pg import AuditPgRepository
    return AuditPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def audit_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repo, raw_conn_or_None)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo, conn, backend
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo, None, backend


# ---------------------------------------------------------------------------
# contract assertions — same SQL questions, same answers
# ---------------------------------------------------------------------------

def test_log_returns_id(audit_repo):
    repo, _, _ = audit_repo
    entry_id = repo.log(user_id="u1", action="auth.login")
    assert isinstance(entry_id, str)
    assert len(entry_id) > 0


def test_log_all_kwargs_round_trip(audit_repo):
    repo, _, _ = audit_repo
    entry_id = repo.log(
        user_id="u1",
        action="registry.update",
        resource="table:web_sessions",
        params={"after": {"cron": "*/15 * * * *"}},
        params_before={"cron": "0 */1 * * *"},
        client_ip="10.0.0.42",
        client_kind="web",
        correlation_id="corr-123",
        result="success",
        duration_ms=42,
    )
    rows, _cursor = repo.query(correlation_id="corr-123", limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == entry_id
    assert row["user_id"] == "u1"
    assert row["action"] == "registry.update"
    assert row["resource"] == "table:web_sessions"
    assert row["client_ip"] == "10.0.0.42"
    assert row["client_kind"] == "web"
    assert row["correlation_id"] == "corr-123"
    assert row["result"] == "success"
    assert row["duration_ms"] == 42
    # JSON columns normalised to dict on read
    assert _as_dict(row["params"]) == {"after": {"cron": "*/15 * * * *"}}
    assert _as_dict(row["params_before"]) == {"cron": "0 */1 * * *"}


def test_query_time_range(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="a.1")
    repo.log(action="a.2")
    # Need actual time-window narrowing; both impls let us set timestamp via
    # implementation-specific paths — just use a wide window to cover all rows.
    rows, _ = repo.query(since=datetime(2000, 1, 1, tzinfo=timezone.utc))
    actions = {r["action"] for r in rows}
    assert {"a.1", "a.2"}.issubset(actions)


def test_query_action_prefix(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="sync.trigger")
    repo.log(action="sync.complete")
    repo.log(action="auth.login")
    rows, _ = repo.query(action_prefix="sync.")
    actions = {r["action"] for r in rows}
    assert actions == {"sync.trigger", "sync.complete"}


def test_query_action_in(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="a")
    repo.log(action="b")
    repo.log(action="c")
    rows, _ = repo.query(action_in=["a", "c"])
    assert {r["action"] for r in rows} == {"a", "c"}


def test_query_filter_by_user(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="x")
    repo.log(user_id="u2", action="x")
    rows, _ = repo.query(user_id="u1")
    assert len(rows) == 1
    assert rows[0]["user_id"] == "u1"


def test_query_filter_by_resource(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="x", resource="table:a")
    repo.log(action="x", resource="table:b")
    rows, _ = repo.query(resource="table:a")
    assert len(rows) == 1
    assert rows[0]["resource"] == "table:a"


def test_query_result_pattern(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="x", result="success")
    repo.log(action="x", result="error.timeout")
    repo.log(action="x", result="error.permission")
    rows, _ = repo.query(result_pattern="error.%")
    assert {r["result"] for r in rows} == {"error.timeout", "error.permission"}


def test_query_full_text_q(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="x", params={"sql": "SELECT * FROM finance"})
    repo.log(action="x", params={"sql": "SELECT * FROM marketing"})
    rows, _ = repo.query(q="finance")
    assert len(rows) == 1


def test_query_ordering_newest_first(audit_repo):
    """Both impls must order by (timestamp DESC, id DESC)."""
    repo, _, _ = audit_repo
    import time
    repo.log(action="first")
    time.sleep(0.01)
    repo.log(action="second")
    time.sleep(0.01)
    repo.log(action="third")
    rows, _ = repo.query()
    actions_seen = [r["action"] for r in rows]
    # Most recent first
    assert actions_seen[0] == "third"
    assert actions_seen[-1] == "first"


def test_query_actions_helper(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="a")
    repo.log(action="b")
    repo.log(action="c")
    rows = repo.query_actions(["a", "c"], limit=10)
    assert {r["action"] for r in rows} == {"a", "c"}


def test_query_for_resources_helper(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="x", resource="store_submission:abc")
    repo.log(action="y", resource="store_submission:abc")
    repo.log(action="z", resource="store_submission:def")
    rows = repo.query_for_resources(["store_submission:abc"], limit=10)
    assert all(r["resource"] == "store_submission:abc" for r in rows)
    assert len(rows) == 2


def test_query_cursor_pagination(audit_repo):
    repo, _, _ = audit_repo
    import time
    for i in range(5):
        repo.log(action=f"a.{i}")
        time.sleep(0.005)
    page1, c1 = repo.query(limit=2)
    assert len(page1) == 2
    assert c1 is not None
    page2, c2 = repo.query(limit=2, cursor=c1)
    assert len(page2) == 2
    page3, c3 = repo.query(limit=2, cursor=c2)
    assert len(page3) == 1
    assert c3 is None
    all_ids = {r["id"] for r in page1 + page2 + page3}
    assert len(all_ids) == 5


# ---------------------------------------------------------------------------
# aggregates — count_for_user / query_governance / facets / kpis
# ---------------------------------------------------------------------------

def test_count_for_user(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="a")
    repo.log(user_id="u1", action="b")
    repo.log(user_id="u2", action="c")
    assert repo.count_for_user("u1") == 2
    assert repo.count_for_user("u2") == 1
    assert repo.count_for_user("nobody") == 0


def test_query_governance_dual_prefix(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="corporate_memory.write")
    repo.log(action="km_write")  # legacy prefix
    repo.log(action="auth.login")  # neither prefix
    rows = repo.query_governance(limit=50)
    actions = {r["action"] for r in rows}
    assert actions == {"corporate_memory.write", "km_write"}


def test_query_governance_action_filter(audit_repo):
    repo, _, _ = audit_repo
    repo.log(action="corporate_memory.write")
    repo.log(action="km_write")
    repo.log(action="corporate_memory.delete")
    repo.log(action="km_delete")
    rows = repo.query_governance(action="write", limit=50)
    actions = {r["action"] for r in rows}
    assert actions == {"corporate_memory.write", "km_write"}


def test_query_governance_offset_paging(audit_repo):
    repo, _, _ = audit_repo
    import time
    for i in range(5):
        repo.log(action=f"corporate_memory.evt_{i}")
        time.sleep(0.005)
    page1 = repo.query_governance(limit=2, offset=0)
    page2 = repo.query_governance(limit=2, offset=2)
    page3 = repo.query_governance(limit=2, offset=4)
    assert len(page1) == 2
    assert len(page2) == 2
    assert len(page3) == 1
    seen = {r["id"] for r in page1 + page2 + page3}
    assert len(seen) == 5


def test_facets_group_buckets(audit_repo):
    repo, _, _ = audit_repo
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    repo.log(user_id="u1", action="a", resource="r1", result="success", client_kind="web")
    repo.log(user_id="u1", action="a", resource="r1", result="success", client_kind="web")
    repo.log(user_id="u2", action="b", resource="r2", result="error.x", client_kind="cli")
    repo.log(user_id=None, action="run_corporate_memory")  # scheduler via action fallback
    sched = ["run_corporate_memory", "marketplace.sync_all"]
    out = repo.facets(since=since, scheduler_actions=sched, limit=50)
    assert set(out.keys()) == {"users", "actions", "results", "resources", "sources"}
    user_counts = {u["id"]: u["count"] for u in out["users"]}
    assert user_counts["u1"] == 2
    assert user_counts["u2"] == 1
    action_counts = {a["value"]: a["count"] for a in out["actions"]}
    assert action_counts["a"] == 2
    sources = {s["value"]: s["count"] for s in out["sources"]}
    assert sources.get("web") == 2
    assert sources.get("cli") == 1
    assert sources.get("scheduler") == 1


def test_kpis(audit_repo):
    repo, _, _ = audit_repo
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    repo.log(user_id="u1", action="a", result="success", duration_ms=100)
    repo.log(user_id="u1", action="b", result="success", duration_ms=200)
    repo.log(user_id="u2", action="c", result="error.timeout", duration_ms=300)
    repo.log(user_id=None, action="sys", result="success", duration_ms=400)
    out = repo.kpis(since=since)
    assert out["events_total"] == 4
    assert out["active_users"] == 2  # u1, u2 (NULL user excluded)
    assert out["errors"] == 1
    # p95 differs between approx_quantile (DuckDB) and percentile_cont (PG);
    # both should land in the upper range of {100,200,300,400}.
    assert out["p95"] is not None
    assert 250 <= out["p95"] <= 400


# ---------------------------------------------------------------------------
# last_scheduler_tick / active_users_since — Activity Center health pulse
# ---------------------------------------------------------------------------


def test_last_scheduler_tick_none_when_no_matching_rows(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="other_action", result="success")
    assert repo.last_scheduler_tick() is None


def test_last_scheduler_tick_matches_run_prefix_or_marketplace_sync(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="run_session_processor", result="success")
    repo.log(user_id="u1", action="marketplace.sync_all", result="success")
    repo.log(user_id="u1", action="unrelated", result="success")
    assert repo.last_scheduler_tick() is not None


def test_active_users_since_counts_distinct_non_null_user_ids(audit_repo):
    repo, _, _ = audit_repo
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    repo.log(user_id="u1", action="a", result="success")
    repo.log(user_id="u1", action="b", result="success")
    repo.log(user_id="u2", action="c", result="success")
    repo.log(user_id=None, action="sys", result="success")
    assert repo.active_users_since(since) == 2


def test_active_users_since_excludes_rows_before_window(audit_repo):
    repo, _, _ = audit_repo
    repo.log(user_id="u1", action="a", result="success")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert repo.active_users_since(future) == 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _as_dict(v):
    """Normalize ``params``/``params_before`` to a dict for cross-backend
    comparison. DuckDB JSON returns the parsed value; SQLAlchemy with
    psycopg returns dict too, but we accept str for safety."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        return json.loads(v)
    return v
