"""Postgres-side smoke + invariant tests for the config cluster:
metric_definitions, instance_templates (via ClaudeMdTemplate), personal_access_tokens.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def config_engine(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    return db_pg.get_engine()


# ---------------------------------------------------------------------------
# metric_definitions
# ---------------------------------------------------------------------------

def test_metrics_create_and_get(config_engine):
    from src.repositories.metrics_pg import MetricPgRepository

    repo = MetricPgRepository(config_engine)
    repo.create(
        id="m1",
        name="revenue",
        display_name="Revenue",
        category="financial",
        sql="SELECT SUM(amount) FROM orders",
        unit="USD",
        synonyms=["sales", "turnover"],
        tables=["orders", "transactions"],
    )
    row = repo.get("m1")
    assert row["name"] == "revenue"
    assert row["display_name"] == "Revenue"
    assert row["category"] == "financial"
    assert row["synonyms"] == ["sales", "turnover"]
    assert row["tables"] == ["orders", "transactions"]


def test_metrics_upsert_on_create(config_engine):
    from src.repositories.metrics_pg import MetricPgRepository

    repo = MetricPgRepository(config_engine)
    repo.create(id="m1", name="x", display_name="X", category="c", sql="SELECT 1")
    repo.create(id="m1", name="x_v2", display_name="X v2", category="c", sql="SELECT 2")
    assert repo.get("m1")["display_name"] == "X v2"


def test_metrics_find_by_table_and_synonym(config_engine):
    from src.repositories.metrics_pg import MetricPgRepository

    repo = MetricPgRepository(config_engine)
    repo.create(
        id="m1", name="metric1", display_name="M1", category="c",
        sql="SELECT 1", table_name="orders", synonyms=["sales"],
    )
    repo.create(
        id="m2", name="metric2", display_name="M2", category="c",
        sql="SELECT 1", tables=["orders", "events"], synonyms=["events"],
    )
    repo.create(
        id="m3", name="metric3", display_name="M3", category="c",
        sql="SELECT 1", table_name="users",
    )

    by_table = repo.find_by_table("orders")
    assert {r["id"] for r in by_table} == {"m1", "m2"}

    by_synonym = repo.find_by_synonym("sales")
    assert {r["id"] for r in by_synonym} == {"m1"}


def test_metrics_get_table_map_includes_both_columns(config_engine):
    from src.repositories.metrics_pg import MetricPgRepository

    repo = MetricPgRepository(config_engine)
    repo.create(
        id="m1", name="metric1", display_name="M1", category="c",
        sql="SELECT 1", table_name="orders",
    )
    repo.create(
        id="m2", name="metric2", display_name="M2", category="c",
        sql="SELECT 1", tables=["orders", "events"],
    )
    table_map = repo.get_table_map()
    # `orders` appears in both — should contain both metric names
    assert sorted(table_map["orders"]) == ["metric1", "metric2"]
    assert sorted(table_map["events"]) == ["metric2"]


def test_metrics_update_partial(config_engine):
    from src.repositories.metrics_pg import MetricPgRepository

    repo = MetricPgRepository(config_engine)
    repo.create(
        id="m1", name="metric1", display_name="M1", category="c",
        sql="SELECT 1", unit="USD",
    )
    updated = repo.update("m1", display_name="M1 prime", validation={"min": 0})
    assert updated["display_name"] == "M1 prime"
    assert updated["unit"] == "USD"  # untouched
    # JSON column round-trips as dict
    assert updated["validation"] == {"min": 0}


def test_metrics_delete(config_engine):
    from src.repositories.metrics_pg import MetricPgRepository

    repo = MetricPgRepository(config_engine)
    repo.create(id="m1", name="x", display_name="X", category="c", sql="SELECT 1")
    assert repo.delete("m1") is True
    assert repo.delete("m1") is False  # idempotent on missing


# ---------------------------------------------------------------------------
# instance_templates via ClaudeMdTemplate
# ---------------------------------------------------------------------------

def test_claude_md_template_get_creates_default(config_engine):
    from src.repositories.claude_md_template_pg import ClaudeMdTemplatePgRepository

    repo = ClaudeMdTemplatePgRepository(config_engine)
    row = repo.get()
    assert row == {"id": 1, "content": None, "updated_at": None, "updated_by": None}
    # Subsequent get returns the seeded row
    row2 = repo.get()
    assert row2["id"] == 1


def test_claude_md_template_set_and_reset(config_engine):
    from src.repositories.claude_md_template_pg import ClaudeMdTemplatePgRepository

    repo = ClaudeMdTemplatePgRepository(config_engine)
    repo.set("# Hello", updated_by="admin@example.com")
    row = repo.get()
    assert row["content"] == "# Hello"
    assert row["updated_by"] == "admin@example.com"

    repo.set("# Hello v2", updated_by="admin@example.com")
    row = repo.get()
    assert row["content"] == "# Hello v2"

    repo.reset(updated_by="admin@example.com")
    row = repo.get()
    assert row["content"] is None


# ---------------------------------------------------------------------------
# personal_access_tokens
# ---------------------------------------------------------------------------

def test_access_token_create_get_revoke(config_engine):
    from src.repositories.access_tokens_pg import AccessTokenPgRepository

    repo = AccessTokenPgRepository(config_engine)
    repo.create(
        id="t1",
        user_id="u1",
        name="local-dev",
        token_hash="hash1",
        prefix="agt_abc",
    )
    row = repo.get_by_id("t1")
    assert row["user_id"] == "u1"
    assert row["name"] == "local-dev"
    assert row["revoked_at"] is None

    repo.revoke("t1")
    row = repo.get_by_id("t1")
    assert row["revoked_at"] is not None


def test_access_token_list_excludes_revoked_when_asked(config_engine):
    from src.repositories.access_tokens_pg import AccessTokenPgRepository

    repo = AccessTokenPgRepository(config_engine)
    repo.create(id="t1", user_id="u1", name="a", token_hash="h", prefix="p1")
    repo.create(id="t2", user_id="u1", name="b", token_hash="h", prefix="p2")
    repo.revoke("t1")

    all_tokens = repo.list_for_user("u1", include_revoked=True)
    assert {r["id"] for r in all_tokens} == {"t1", "t2"}

    active_only = repo.list_for_user("u1", include_revoked=False)
    assert {r["id"] for r in active_only} == {"t2"}


def test_access_token_mark_used(config_engine):
    from src.repositories.access_tokens_pg import AccessTokenPgRepository

    repo = AccessTokenPgRepository(config_engine)
    repo.create(id="t1", user_id="u1", name="a", token_hash="h", prefix="p1")
    repo.mark_used("t1", ip="10.0.0.1")
    row = repo.get_by_id("t1")
    assert row["last_used_ip"] == "10.0.0.1"
    assert row["last_used_at"] is not None


def test_access_token_list_all_with_user_join(config_engine):
    from src.repositories.access_tokens_pg import AccessTokenPgRepository
    from src.repositories.users_pg import UsersPgRepository

    users = UsersPgRepository(config_engine)
    tokens = AccessTokenPgRepository(config_engine)

    users.create(id="u1", email="alice@example.com", name="Alice")
    tokens.create(id="t1", user_id="u1", name="a", token_hash="h", prefix="p")
    tokens.create(id="t-ghost", user_id="u-nonexistent", name="b", token_hash="h", prefix="p2")

    rows = tokens.list_all_with_user()
    by_id = {r["id"]: r for r in rows}
    assert by_id["t1"]["user_email"] == "alice@example.com"
    assert by_id["t-ghost"]["user_email"] is None  # LEFT JOIN preserves orphan tokens
