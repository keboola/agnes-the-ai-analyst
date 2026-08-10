"""Alembic 0061 (DuckDB v115) — reclassify pre-existing governance agents.

Mirrors ``tests/test_db_schema_version.py``'s ``_v114_to_v115`` coverage for
the Postgres ladder: a ``draft`` row whose ``id`` does NOT carry the
builder's ``agt_`` prefix (and is not the seeded default agent) moves to
``ready``; every ``agt_``-prefixed builder row and the default agent stay
put, regardless of slug — including a builder draft the user already named,
which a slug-only discriminator got wrong (see
``test_0061_promotes_by_id_prefix_not_slug``).
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


def _builder_id() -> str:
    """A builder-created row's id shape: ``app/api/agents.py::create_agent``
    always mints ``"agt_" + uuid4().hex``, named or not."""
    return "agt_" + uuid.uuid4().hex


def test_0061_reclassifies_pre_existing_governance_agents_as_ready(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0060_pkg_publisher_v113")

    with pg_engine.begin() as conn:
        _insert_agent(conn, id=str(uuid.uuid4()), slug="revenue-bot", status="draft", name="Revenue Bot")
        _insert_agent(conn, id=_builder_id(), slug="agent", status="draft", name="")
        _insert_agent(conn, id=_builder_id(), slug="agent-2", status="draft", name="")
        _insert_agent(conn, id=_builder_id(), slug="finance-bot", status="draft", name="Finance Bot")
        _insert_agent(conn, id=str(uuid.uuid4()), slug="default", status="draft", is_default=True, name="Default")
        _insert_agent(conn, id=str(uuid.uuid4()), slug="already-ready", status="ready", name="Already Ready")

    command.upgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.connect() as conn:
        rows = dict(conn.execute(sa.text("SELECT slug, status FROM agents")).all())

    assert rows["revenue-bot"] == "ready", "a governance-created agent's slug is deliberately chosen — freeze it"
    assert rows["agent"] == "draft", "an unnamed builder placeholder must keep re-deriving its slug"
    assert rows["agent-2"] == "draft", "…including a suffixed placeholder"
    assert rows["finance-bot"] == "draft", (
        "a builder draft already named by the user is still unpublished — a slug-based "
        "discriminator would wrongly freeze it because 'finance-bot' isn't placeholder-shaped"
    )
    assert rows["default"] == "draft", "the seeded default agent is a PERMANENT draft by design"
    assert rows["already-ready"] == "ready", "already-ready rows are untouched"


def test_0061_promotes_by_id_prefix_not_slug(pg_engine):
    """Regression pin: a builder-created draft (``agt_``-prefixed id) that
    the user already gave a real, non-placeholder-shaped name/slug must stay
    ``draft`` — it is not yet published, so promoting it here would
    permanently freeze an address for an agent nobody ever marked ready."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0060_pkg_publisher_v113")

    agent_id = _builder_id()
    with pg_engine.begin() as conn:
        _insert_agent(conn, id=agent_id, slug="finance-bot", status="draft", name="Finance Bot")

    command.upgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.connect() as conn:
        status = conn.execute(sa.text("SELECT status FROM agents WHERE id = :id"), {"id": agent_id}).scalar()
    assert status == "draft"


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
