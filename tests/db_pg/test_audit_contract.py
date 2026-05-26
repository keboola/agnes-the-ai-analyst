"""Postgres-only contract tests for the audit repository.

Originally parametrised over [DuckDB, Postgres] for the dual-write
window. The DuckDB side is retired post-cutover (the legacy
``src/repositories/audit.py`` module and ``src/db._ensure_schema``
are gone). Tests stay PG-only and keep the same assertion bodies.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest


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


@pytest.fixture
def audit_repo(tmp_path, pg_engine, monkeypatch):
    """Yields ``(repo, None, 'pg')`` — signature kept for back-compat
    with the parametrised callers that unpack three values.
    """
    repo, _ = _make_pg_repo(pg_engine, monkeypatch)
    yield repo, None, "pg"


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
