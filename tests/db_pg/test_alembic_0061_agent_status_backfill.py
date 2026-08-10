"""Alembic 0061 (DuckDB v115) — reclassify pre-existing governance agents.

Mirrors ``tests/test_db_schema_version.py``'s ``_v114_to_v115`` coverage for
the Postgres ladder: a ``draft`` row with a caller-chosen slug (not the
builder's placeholder lineage, not the seeded default agent) moves to
``ready``; a placeholder-lineage draft and the default agent stay put.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def _insert_agent(conn, *, id, slug, status, is_default=False, name="Agent"):
    conn.execute(
        sa.text(
            "INSERT INTO agents (id, owner_user_id, name, slug, status, is_default, created_at, updated_at) "
            "VALUES (:id, 'u1', :name, :slug, :status, :is_default, now(), now())"
        ),
        {"id": id, "name": name, "slug": slug, "status": status, "is_default": is_default},
    )


def test_0061_reclassifies_pre_existing_governance_agents_as_ready(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0060_pkg_publisher_v113")

    with pg_engine.begin() as conn:
        _insert_agent(conn, id=str(uuid.uuid4()), slug="revenue-bot", status="draft", name="Revenue Bot")
        _insert_agent(conn, id=str(uuid.uuid4()), slug="agent", status="draft", name="")
        _insert_agent(conn, id=str(uuid.uuid4()), slug="agent-2", status="draft", name="")
        _insert_agent(conn, id=str(uuid.uuid4()), slug="default", status="draft", is_default=True, name="Default")
        _insert_agent(conn, id=str(uuid.uuid4()), slug="already-ready", status="ready", name="Already Ready")

    command.upgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.connect() as conn:
        rows = dict(conn.execute(sa.text("SELECT slug, status FROM agents")).all())

    assert rows["revenue-bot"] == "ready", "a governance-created agent's slug is deliberately chosen — freeze it"
    assert rows["agent"] == "draft", "an unnamed builder placeholder must keep re-deriving its slug"
    assert rows["agent-2"] == "draft", "…including a suffixed placeholder"
    assert rows["default"] == "draft", "the seeded default agent is a PERMANENT draft by design"
    assert rows["already-ready"] == "ready", "already-ready rows are untouched"


def test_0061_noops_gracefully_when_agents_table_is_absent(pg_engine):
    """Alembic runs before the app's first boot on a fresh install, so the
    ``agents`` table may not exist yet if this migration somehow ran ahead
    of the table's own creation. Must not raise."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0060_pkg_publisher_v113")

    with pg_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS agents CASCADE"))

    command.upgrade(cfg, "0061_agent_status_backfill_v115")  # must not raise


def test_0061_downgrade_is_a_documented_noop(pg_engine):
    """The backfill cannot be safely inverted (no marker distinguishes a
    flipped row from one made ready afterward through ordinary use) — the
    downgrade leaves data untouched rather than guessing."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0060_pkg_publisher_v113")

    agent_id = str(uuid.uuid4())
    with pg_engine.begin() as conn:
        _insert_agent(conn, id=agent_id, slug="revenue-bot", status="draft", name="Revenue Bot")

    command.upgrade(cfg, "0061_agent_status_backfill_v115")
    command.downgrade(cfg, "0060_pkg_publisher_v113")

    with pg_engine.connect() as conn:
        status = conn.execute(sa.text("SELECT status FROM agents WHERE id = :id"), {"id": agent_id}).scalar()
    assert status == "ready", "downgrade must not revert the backfill it cannot safely undo"
