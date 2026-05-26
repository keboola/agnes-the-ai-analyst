"""Tests for src/repositories/__init__.py factory.

The factory picks DuckDB or Postgres repo classes based on
``AGNES_DB_URL`` env var. Every callsite imports through this module so
the choice happens at one place, not 99 callsites.

Each factory function returns a ready-to-use repository instance —
caller does not pass a connection or engine. The factory pulls the
right one from the singleton in src.db / src.db_pg.
"""
from __future__ import annotations

import os

import duckdb
import pytest


_FACTORY_NAMES = (
    # Core user / RBAC cluster
    "users_repo",
    "user_groups_repo",
    "user_group_members_repo",
    "resource_grants_repo",
    "audit_repo",
    # Ops cluster
    "table_registry_repo",
    "sync_state_repo",
    # Config / templates
    "metric_repo",
    "claude_md_template_repo",
    "welcome_template_repo",
    "news_template_repo",
    "access_token_repo",
    "profile_repo",
    # Lookup / cache
    "view_ownership_repo",
    "column_metadata_repo",
    "bq_metadata_cache_repo",
    "sync_settings_repo",
    "notifications_telegram_repo",
    "notifications_pending_code_repo",
    "notifications_script_repo",
    # Telemetry
    "session_processor_state_repo",
    "observability_views_repo",
    "usage_repo",
    # Store / marketplace
    "marketplace_registry_repo",
    "marketplace_plugins_repo",
    "store_entities_repo",
    "user_store_installs_repo",
    "user_curated_subscriptions_repo",
    "store_submissions_repo",
    # Knowledge
    "knowledge_repo",
)


def test_factory_exports_every_repo_function():
    """Public contract: one factory per repository class."""
    import src.repositories as r
    for name in _FACTORY_NAMES:
        assert hasattr(r, name), f"src.repositories.{name} missing"


def test_factory_raises_when_url_unset(monkeypatch):
    """Post-cutover: unset AGNES_DB_URL → repos raise on first use.

    src.db_pg.get_engine() refuses to default to a local SQLite or
    DuckDB stand-in; a missing URL is an explicit configuration error.
    """
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import importlib
    import src.db_pg as db_pg
    import src.repositories
    db_pg.dispose()
    importlib.reload(src.repositories)

    with pytest.raises(RuntimeError, match="Postgres URL is unset"):
        src.repositories.users_repo()


def test_factory_picks_pg_when_url_set(_pg_url, monkeypatch):
    """AGNES_DB_URL set → Postgres-backed repos."""
    monkeypatch.setenv("AGNES_DB_URL", _pg_url)

    # Run alembic so the PG side has the schema
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = _pg_url
    command.upgrade(cfg, "head")

    import src.db_pg as db_pg
    db_pg.dispose()

    import importlib
    import src.repositories
    importlib.reload(src.repositories)

    repo = src.repositories.users_repo()
    assert repo.__class__.__name__ == "UsersPgRepository"
    # Smoke: actually works
    repo.create(id="u1", email="alice@example.com", name="Alice")
    assert repo.get_by_id("u1")["email"] == "alice@example.com"


def test_factory_audit_repo_round_trip_pg(_pg_url, monkeypatch):
    """Smoke: audit_repo() instance writes + reads a row against PG."""
    import importlib
    import src.repositories

    monkeypatch.setenv("AGNES_DB_URL", _pg_url)
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = _pg_url
    command.upgrade(cfg, "head")

    import src.db_pg as db_pg
    db_pg.dispose()
    importlib.reload(src.repositories)

    pg_repo = src.repositories.audit_repo()
    pg_repo.log(user_id="u1", action="auth.login", correlation_id="c-pg")
    rows, _ = pg_repo.query(correlation_id="c-pg", limit=10)
    assert len(rows) == 1
    assert rows[0]["action"] == "auth.login"
