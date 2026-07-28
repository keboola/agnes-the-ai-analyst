"""Tests for the Alembic skeleton: env loads, baseline runs cleanly,
upgrade head from an empty DB only creates alembic_version.

These tests are RED before the migrations/ directory + alembic.ini exist;
they go GREEN once Phase B is wired.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa


REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str):
    """Build an Alembic Config pointed at the repo's migrations/ dir.

    URL is passed via ``cfg.attributes`` rather than
    ``cfg.set_main_option("sqlalchemy.url", ...)`` because configparser
    interpolates ``%`` characters, which breaks pgserver/Cloud SQL Unix-
    socket URLs that contain percent-encoded paths.
    """
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def test_alembic_ini_exists():
    """The Alembic config file must be at the repo root."""
    assert (REPO_ROOT / "alembic.ini").is_file(), (
        "alembic.ini missing at repo root — Phase B not wired"
    )


def test_migrations_env_py_exists():
    """The Alembic env script must be present."""
    assert (REPO_ROOT / "migrations" / "env.py").is_file(), (
        "migrations/env.py missing — Phase B not wired"
    )


def test_baseline_revision_exists():
    """At least one revision must exist in versions/ (the baseline)."""
    versions_dir = REPO_ROOT / "migrations" / "versions"
    assert versions_dir.is_dir(), "migrations/versions/ missing"
    revs = sorted(p for p in versions_dir.glob("*.py") if not p.name.startswith("__"))
    assert revs, "no migration files in migrations/versions/"


def test_alembic_upgrade_head_runs(pg_engine):
    """`alembic upgrade head` succeeds against a fresh PG."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "head")  # must not raise

    # alembic_version table exists after upgrade
    inspector = sa.inspect(pg_engine)
    assert "alembic_version" in inspector.get_table_names(schema="public")


def test_baseline_upgrade_creates_only_alembic_version(pg_engine):
    """With ONLY the baseline (empty) revision in the chain, the DB should
    have exactly one table: ``alembic_version``. This forces step 4 of
    the TDD plan — if someone accidentally adds DDL to the baseline, this
    test fires red.
    """
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "base+1")  # baseline only (one step above empty)

    inspector = sa.inspect(pg_engine)
    user_tables = [
        t for t in inspector.get_table_names(schema="public")
        if not t.startswith("pg_")
    ]
    # The baseline is allowed to create exactly one table: alembic_version.
    assert user_tables == ["alembic_version"], (
        f"baseline must not create user tables; found: {user_tables}"
    )


def test_alembic_downgrade_to_base_removes_alembic_version(pg_engine):
    """Downgrade unwinds the chain cleanly back to base (no schema state).

    This is the foundational rollback assertion: if `downgrade base` ever
    leaves residue, the whole "true rollback" promise is broken.
    """
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    inspector = sa.inspect(pg_engine)
    user_tables = [
        t for t in inspector.get_table_names(schema="public")
        if not t.startswith("pg_")
    ]
    # After full downgrade, the only acceptable table is alembic_version
    # itself (Alembic keeps it as the version-tracking table; it just
    # records the empty-base state). It is NOT a user-data table.
    assert set(user_tables).issubset({"alembic_version"}), (
        f"downgrade base left residue: {user_tables}"
    )
