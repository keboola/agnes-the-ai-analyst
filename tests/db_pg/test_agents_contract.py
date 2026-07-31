"""Cross-engine contract tests for the agents repository."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.agents import AgentsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return AgentsRepository(conn), conn


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

    from src.repositories.agents_pg import AgentsPgRepository

    return AgentsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_create_get_roundtrip(repo):
    repo.create(id="a1", owner_user_id="u1", name="Sales reporter", slug="sales-reporter")
    row = repo.get_by_slug("u1", "sales-reporter")
    assert row["id"] == "a1" and row["plugins_mode"] == "all"
    assert row["memory_write_mode"] == "propose"


def test_slug_unique_per_owner(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    with pytest.raises(Exception):
        repo.create(id="a2", owner_user_id="u1", name="B", slug="x")
    repo.create(id="a3", owner_user_id="u2", name="C", slug="x")  # other owner OK


def test_soft_delete_tombstones_slug(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.soft_delete("a1")
    assert repo.get_by_slug("u1", "x") is None  # invisible to runtime
    assert repo.get_by_id("a1")["deleted_at"] is not None
    with pytest.raises(Exception):  # slug never reused
        repo.create(id="a2", owner_user_id="u1", name="B", slug="x")


def test_get_or_create_default_idempotent(repo):
    d1 = repo.get_or_create_default("u1")
    d2 = repo.get_or_create_default("u1")
    assert d1["id"] == d2["id"] and d1["is_default"] is True
    assert len(repo.list_for_user("u1")) == 1


def test_scope_replace_all(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.set_scope("a1", [("plugin", "p1"), ("table", "t1")])
    repo.set_scope("a1", [("plugin", "p2")])
    assert repo.get_scope("a1") == [{"item_type": "plugin", "item_id": "p2"}]


def test_update_whitelist(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.update("a1", name="B", model="claude-sonnet-5", plugins_mode="selected")
    row = repo.get_by_id("a1")
    assert row["name"] == "B" and row["plugins_mode"] == "selected"
    with pytest.raises(ValueError):
        repo.update("a1", owner_user_id="u2")  # not whitelisted


def test_builder_superset_roundtrip(repo):
    """v111 paper-theme builder superset — create + update + read the authored
    fields on the same canonical row that holds main's agent-as-API columns."""
    repo.create(
        id="a1",
        owner_user_id="u1",
        name="Analyst",
        slug="analyst",
        system_prompt="be precise",
        role="data analyst",
        tone="warm",
        greeting="hi there",
        knowledge='["k1", "k2"]',
        plugins='["p1"]',
        surfaces='{"chat": true}',
        status="ready",
    )
    row = repo.get_by_id("a1")
    assert row["role"] == "data analyst"
    assert row["tone"] == "warm"
    assert row["greeting"] == "hi there"
    assert row["knowledge"] == '["k1", "k2"]'
    assert row["plugins"] == '["p1"]'
    assert row["surfaces"] == '{"chat": true}'
    assert row["status"] == "ready"
    # The builder maps instructions -> system_prompt on the same table.
    assert row["system_prompt"] == "be precise"

    # Superset columns are whitelisted for update; the JSON payloads are opaque.
    repo.update("a1", role="senior analyst", knowledge='["k3"]', status="draft")
    row = repo.get_by_id("a1")
    assert row["role"] == "senior analyst"
    assert row["knowledge"] == '["k3"]'
    assert row["status"] == "draft"


def test_builder_defaults_and_slug_picker(repo):
    """A create with no builder fields lands the column DEFAULTs, and the slug
    picker sees tombstones via include_deleted."""
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    row = repo.get_by_id("a1")
    assert row["tone"] == "concise"
    assert row["knowledge"] == "[]"
    assert row["surfaces"] == "{}"
    assert row["status"] == "draft"
    repo.soft_delete("a1")
    assert repo.get_by_slug("u1", "x") is None
    assert repo.get_by_slug("u1", "x", include_deleted=True) is not None


def test_scope_snapshot_roundtrip(repo):
    repo.create(id="a1", owner_user_id="u1", name="A", slug="x")
    repo.record_scope_snapshot(id="s1", session_id="c1", agent_id="a1", effective_scope='{"tables": ["t1"]}')
    snaps = repo.list_scope_snapshots("c1")
    assert len(snaps) == 1 and snaps[0]["effective_scope"] == '{"tables": ["t1"]}'
