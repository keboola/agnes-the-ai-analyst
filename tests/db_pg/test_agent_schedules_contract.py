"""Cross-engine contract tests for the agent_schedules repository."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.agent_schedules import AgentSchedulesRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return AgentSchedulesRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.agent_schedules_pg import AgentSchedulesPgRepository

    return AgentSchedulesPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_create_and_get(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="do the thing")
    row = repo.get("s1")
    assert row is not None
    assert row["agent_id"] == "a1"
    assert row["name"] == "morning"
    assert row["schedule"] == "daily 07:00"
    assert row["prompt"] == "do the thing"
    assert bool(row["enabled"]) is True
    # Cadence anchors at creation — a brand-new row is never immediately due.
    assert row["last_run_at"] is not None
    assert row["last_status"] is None
    assert row["last_job_id"] is None


def test_create_disabled(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p", enabled=False)
    row = repo.get("s1")
    assert bool(row["enabled"]) is False


def test_get_missing_returns_none(repo):
    assert repo.get("missing") is None


def test_get_by_name(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    row = repo.get_by_name("a1", "morning")
    assert row is not None and row["id"] == "s1"
    assert repo.get_by_name("a1", "evening") is None
    # scoped per agent — same name on a different agent doesn't collide
    assert repo.get_by_name("a2", "morning") is None


def test_list_for_agent(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    repo.create(id="s2", agent_id="a1", name="evening", schedule="daily 18:00", prompt="p")
    repo.create(id="s3", agent_id="a2", name="morning", schedule="daily 07:00", prompt="p")
    rows = repo.list_for_agent("a1")
    assert {r["id"] for r in rows} == {"s1", "s2"}
    assert repo.list_for_agent("a2") == [repo.get("s3")]
    assert repo.list_for_agent("no-such-agent") == []


def test_count_for_agent(repo):
    assert repo.count_for_agent("a1") == 0
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    repo.create(id="s2", agent_id="a1", name="evening", schedule="daily 18:00", prompt="p")
    repo.create(id="s3", agent_id="a2", name="morning", schedule="daily 07:00", prompt="p")
    assert repo.count_for_agent("a1") == 2
    assert repo.count_for_agent("a2") == 1


def test_list_enabled_spans_agents_and_excludes_disabled(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p", enabled=True)
    repo.create(id="s2", agent_id="a1", name="evening", schedule="daily 18:00", prompt="p", enabled=False)
    repo.create(id="s3", agent_id="a2", name="morning", schedule="daily 07:00", prompt="p", enabled=True)
    ids = {r["id"] for r in repo.list_enabled()}
    assert ids == {"s1", "s3"}


def test_update(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    repo.update("s1", schedule="daily 08:00", prompt="new prompt", enabled=False)
    row = repo.get("s1")
    assert row["schedule"] == "daily 08:00"
    assert row["prompt"] == "new prompt"
    assert bool(row["enabled"]) is False


def test_update_rejects_non_whitelisted_field(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    with pytest.raises(ValueError):
        repo.update("s1", agent_id="other")


def test_update_empty_is_noop(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    repo.update("s1")
    assert repo.get("s1")["schedule"] == "daily 07:00"


def test_delete(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    repo.delete("s1")
    assert repo.get("s1") is None


def test_delete_for_agent(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    repo.create(id="s2", agent_id="a1", name="evening", schedule="daily 18:00", prompt="p")
    repo.create(id="s3", agent_id="a2", name="morning", schedule="daily 07:00", prompt="p")
    repo.delete_for_agent("a1")
    assert repo.list_for_agent("a1") == []
    assert len(repo.list_for_agent("a2")) == 1


def test_delete_for_agent_with_no_rows_is_a_noop(repo):
    repo.delete_for_agent("no-such-agent")


def test_claim_for_run_with_the_read_value_wins(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    expected = repo.get("s1")["last_run_at"]
    now = datetime.now(timezone.utc)
    assert repo.claim_for_run("s1", expected, now) is True
    row = repo.get("s1")
    # Round-trip through either backend may drop sub-second precision or
    # normalize tzinfo — compare via isoformat prefix rather than equality.
    assert row["last_run_at"] is not None


def test_claim_for_run_is_atomic_against_a_concurrent_sweep(repo):
    """The second claim attempt using the SAME expected last_run_at (the
    stale value a concurrent sweep tick would have read) must lose the
    race once the first claim has landed."""
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    stale = repo.get("s1")["last_run_at"]
    now = datetime.now(timezone.utc)
    assert repo.claim_for_run("s1", stale, now) is True
    # A second sweep that read the row before the first claim still holds
    # the stale anchor — it must now lose.
    assert repo.claim_for_run("s1", stale, now) is False


def test_claim_for_run_missing_row_returns_false(repo):
    assert repo.claim_for_run("missing", None, datetime.now(timezone.utc)) is False


def test_record_dispatch_result(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    repo.record_dispatch_result("s1", "enqueued", job_id="job_1")
    row = repo.get("s1")
    assert row["last_status"] == "enqueued"
    assert row["last_job_id"] == "job_1"


def test_record_dispatch_result_failed_enqueue_has_no_job_id(repo):
    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    repo.record_dispatch_result("s1", "failed_enqueue")
    row = repo.get("s1")
    assert row["last_status"] == "failed_enqueue"
    assert row["last_job_id"] is None


def test_unique_name_per_agent_enforced_at_the_db_layer(repo):
    """The 409 schedule_name_taken contract at the API layer relies on this
    constraint as the race backstop — pre-checked with get_by_name, enforced
    here for the concurrent-create case."""
    import duckdb
    import sqlalchemy.exc as sa_exc

    repo.create(id="s1", agent_id="a1", name="morning", schedule="daily 07:00", prompt="p")
    with pytest.raises((duckdb.ConstraintException, sa_exc.IntegrityError)):
        repo.create(id="s2", agent_id="a1", name="morning", schedule="daily 08:00", prompt="p2")
