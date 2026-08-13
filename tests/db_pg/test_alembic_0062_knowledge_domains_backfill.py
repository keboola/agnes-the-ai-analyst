"""Alembic 0062 — backfill ``knowledge_item_domains`` from the legacy
``knowledge_items.domain`` scalar column on Postgres.

Devin Review on #1290: PG's ``create()``/``update()`` did not write the
junction row until #1263. Any row created between the junction's
introduction (``0011_data_packages``) and #1263 landing carries a scalar
``domain`` with no matching ``knowledge_item_domains`` row, so the
junction-only ``list_by_domain`` (#1290) can no longer find it. DuckDB
never has this gap: its v49 migration (``src/db.py::_v51_to_v52``) dropped
the scalar column in the SAME migration that first introduced the junction,
backfilling every historical value at that moment — there was never a
window where the junction existed without the write path keeping it in
sync. PG kept the scalar column indefinitely, so its junction-adoption
happened in a separate, later commit, leaving a real gap. This migration
closes it with a one-time backfill; no DuckDB step is needed (see
``src/db.py`` SCHEMA_VERSION comment / CHANGELOG for the rationale).
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


def _insert_legacy_item(conn, *, id, domain, title="Legacy item"):
    """Simulate a pre-#1263 row: scalar ``domain`` set, no junction row —
    bypasses the repo write path entirely (raw INSERT), which is the only
    way to reproduce this shape since the current write path always keeps
    the junction in step."""
    conn.execute(
        sa.text(
            "INSERT INTO knowledge_items (id, title, content, category, status, domain, created_at, updated_at) "
            "VALUES (:id, :title, 'content', 'general', 'approved', :domain, now(), now())"
        ),
        {"id": id, "title": title, "domain": domain},
    )


def test_0062_backfills_junction_for_a_pre_1263_scalar_only_row(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.begin() as conn:
        domain_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO memory_domains (id, slug, name, created_at, updated_at) VALUES (:id, 'ops', 'Ops', now(), now())"
            ),
            {"id": domain_id},
        )
        _insert_legacy_item(conn, id="ki_legacy_ops", domain="ops")

    command.upgrade(cfg, "0062_knowledge_domains_backfill")

    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT domain_id FROM knowledge_item_domains WHERE item_id = :id"),
            {"id": "ki_legacy_ops"},
        ).fetchall()
    assert [r[0] for r in rows] == [domain_id]


def test_0062_creates_a_missing_memory_domain_from_a_free_text_legacy_value(pg_engine):
    """Pre-#1263 ``create()`` had no slug validation at all (any string went
    straight into the scalar column) — so a legacy row's ``domain`` may not
    match any existing ``memory_domains.slug``. Mirrors DuckDB's own v49
    migration (``src/db.py::_v51_to_v52`` step 5), which creates a domain
    row for exactly this case rather than silently dropping the item."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.begin() as conn:
        # Lowercase-with-space, not mixed-case: the normalization formula
        # (mirrors ``src/db.py::_v51_to_v52`` step 5) applies
        # ``regexp_replace`` BEFORE ``lower()``, so an already-shipped quirk
        # there treats uppercase runs as non-alnum too — out of scope for
        # this migration (it would apply identically to the DuckDB step).
        # A lowercase legacy value keeps this test's assertion about slug
        # creation independent of that pre-existing quirk.
        _insert_legacy_item(conn, id="ki_legacy_freeform", domain="q-team notes")

    command.upgrade(cfg, "0062_knowledge_domains_backfill")

    with pg_engine.connect() as conn:
        domain_row = conn.execute(sa.text("SELECT id, name FROM memory_domains WHERE slug = 'q-team-notes'")).fetchone()
        assert domain_row is not None, "expected a memory_domains row to be created for the free-text legacy domain"
        junction_row = conn.execute(
            sa.text("SELECT domain_id FROM knowledge_item_domains WHERE item_id = 'ki_legacy_freeform'")
        ).fetchone()
        assert junction_row is not None and junction_row[0] == domain_row[0]


def test_0062_does_not_touch_rows_already_carrying_a_junction_entry(pg_engine):
    """A row created after #1263 already has BOTH scalar + junction rows in
    sync — the backfill's ``ON CONFLICT DO NOTHING`` must not duplicate or
    otherwise disturb it."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.begin() as conn:
        domain_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO memory_domains (id, slug, name, created_at, updated_at) VALUES (:id, 'ops', 'Ops', now(), now())"
            ),
            {"id": domain_id},
        )
        _insert_legacy_item(conn, id="ki_synced", domain="ops")
        conn.execute(
            sa.text(
                "INSERT INTO knowledge_item_domains (item_id, domain_id, added_by, added_at) "
                "VALUES ('ki_synced', :domain_id, 'system', now())"
            ),
            {"domain_id": domain_id},
        )

    command.upgrade(cfg, "0062_knowledge_domains_backfill")

    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT domain_id FROM knowledge_item_domains WHERE item_id = 'ki_synced'")
        ).fetchall()
    assert [r[0] for r in rows] == [domain_id]


def test_0062_noops_on_a_fresh_install_with_no_legacy_rows(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "head")  # must not raise on an empty knowledge_items table

    with pg_engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM knowledge_item_domains")).scalar()
    assert count == 0


def test_0062_makes_a_legacy_item_visible_via_list_by_domain(pg_engine, monkeypatch):
    """End-to-end proof, through the actual repository call the verification
    detector uses (not just the raw junction table): a pre-#1263 row that
    ``KnowledgePgRepository.list_by_domain`` (#1290, junction-only) could no
    longer see becomes visible again once this backfill has run."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.begin() as conn:
        domain_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO memory_domains (id, slug, name, created_at, updated_at) "
                "VALUES (:id, 'ops', 'Ops', now(), now())"
            ),
            {"id": domain_id},
        )
        _insert_legacy_item(conn, id="ki_legacy_ops", domain="ops")

    command.upgrade(cfg, "0062_knowledge_domains_backfill")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(db_pg.get_engine())
    found = {r["id"] for r in repo.list_by_domain("ops")}
    assert "ki_legacy_ops" in found, "a pre-#1263 scalar-only row must be visible via list_by_domain after the backfill"


def test_0062_downgrade_is_a_documented_noop(pg_engine):
    """Like 0061's agent backfill, this cannot be safely inverted — there is
    no marker distinguishing a junction row this migration wrote from one a
    normal `create()`/`update()` call wrote afterward."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.begin() as conn:
        domain_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO memory_domains (id, slug, name, created_at, updated_at) VALUES (:id, 'ops', 'Ops', now(), now())"
            ),
            {"id": domain_id},
        )
        _insert_legacy_item(conn, id="ki_legacy_ops", domain="ops")

    command.upgrade(cfg, "0062_knowledge_domains_backfill")
    command.downgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT domain_id FROM knowledge_item_domains WHERE item_id = 'ki_legacy_ops'")
        ).fetchall()
    assert [r[0] for r in rows] == [domain_id], "downgrade must not revert the backfill it cannot safely undo"
