"""Cross-engine contract tests for the agent_webhooks repository."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.agent_webhooks import AgentWebhooksRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return AgentWebhooksRepository(conn), conn


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

    from src.repositories.agent_webhooks_pg import AgentWebhooksPgRepository

    return AgentWebhooksPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_create_and_list_for_agent(repo):
    repo.create(
        id="w1",
        agent_id="a1",
        owner_user_id="u1",
        url="https://hook.example.com/x",
        secret="s1",
        events="job.completed,job.failed",
    )
    rows = repo.list_for_agent("a1")
    assert len(rows) == 1 and rows[0]["url"] == "https://hook.example.com/x"


def test_list_active_for_event_filters(repo):
    repo.create(
        id="w1",
        agent_id="a1",
        owner_user_id="u1",
        url="https://h/x",
        secret="s",
        events="job.completed",
    )
    assert len(repo.list_active_for_event("a1", "job.completed")) == 1
    assert repo.list_active_for_event("a1", "job.failed") == []


def test_failure_tracking_and_disable(repo):
    repo.create(
        id="w1",
        agent_id="a1",
        owner_user_id="u1",
        url="https://h/x",
        secret="s",
        events="job.completed",
    )
    assert repo.record_failure("w1") == 1
    assert repo.record_failure("w1") == 2
    repo.record_success("w1")
    assert repo.get("w1")["consecutive_failures"] == 0
    repo.disable("w1")
    assert repo.get("w1")["active"] is False
    assert repo.list_active_for_event("a1", "job.completed") == []


def test_get_missing_returns_none(repo):
    assert repo.get("missing") is None


def test_delete(repo):
    repo.create(
        id="w1",
        agent_id="a1",
        owner_user_id="u1",
        url="https://h/x",
        secret="s",
        events="job.completed",
    )
    repo.delete("w1")
    assert repo.get("w1") is None
    assert repo.list_for_agent("a1") == []


def test_delete_for_agent(repo):
    repo.create(
        id="w1",
        agent_id="a1",
        owner_user_id="u1",
        url="https://h/x",
        secret="s",
        events="job.completed",
    )
    repo.create(
        id="w2",
        agent_id="a1",
        owner_user_id="u1",
        url="https://h/y",
        secret="s",
        events="job.completed",
    )
    repo.create(
        id="w3",
        agent_id="a2",
        owner_user_id="u1",
        url="https://h/z",
        secret="s",
        events="job.completed",
    )
    repo.delete_for_agent("a1")
    assert repo.list_for_agent("a1") == []
    # a different agent's webhooks are untouched
    assert len(repo.list_for_agent("a2")) == 1


def test_delete_for_agent_with_no_rows_is_a_noop(repo):
    repo.delete_for_agent("no-such-agent")
    assert repo.list_for_agent("no-such-agent") == []


def test_list_active_for_event_ignores_disabled(repo):
    repo.create(
        id="w1",
        agent_id="a1",
        owner_user_id="u1",
        url="https://h/x",
        secret="s",
        events="job.completed,job.failed",
    )
    repo.create(
        id="w2",
        agent_id="a1",
        owner_user_id="u1",
        url="https://h/y",
        secret="s",
        events="job.completed",
    )
    repo.disable("w1")
    rows = repo.list_active_for_event("a1", "job.completed")
    assert len(rows) == 1 and rows[0]["id"] == "w2"
