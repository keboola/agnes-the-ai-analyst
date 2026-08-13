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
        _insert_legacy_item(conn, id="ki_legacy_freeform", domain="q-team notes")

    command.upgrade(cfg, "0062_knowledge_domains_backfill")

    with pg_engine.connect() as conn:
        domain_row = conn.execute(sa.text("SELECT id, name FROM memory_domains WHERE slug = 'q-team-notes'")).fetchone()
        assert domain_row is not None, "expected a memory_domains row to be created for the free-text legacy domain"
        junction_row = conn.execute(
            sa.text("SELECT domain_id FROM knowledge_item_domains WHERE item_id = 'ki_legacy_freeform'")
        ).fetchone()
        assert junction_row is not None and junction_row[0] == domain_row[0]


def test_0062_normalizes_a_capitalised_legacy_domain_to_the_canonical_slug(pg_engine):
    """A mixed-case legacy value must resolve to the existing lowercase slug.

    Pre-#1263 ``create()`` did no slug normalization, so ``'Finance'`` is a
    perfectly ordinary scalar value in the wild. Postgres' ``regexp_replace``
    is case-sensitive, so ``[^a-z0-9]`` matches uppercase letters too: applied
    to the RAW value it turns ``'Finance'`` into ``'-inance'`` and only the
    trailing ``lower()`` runs afterwards, minting a junk ``memory_domains``
    row and filing the item under it instead of the real domain — with a
    no-op ``downgrade()``, unrollable. Lower-casing BEFORE the regexp is what
    makes the formula produce the slug shape the seed uses.
    """
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.begin() as conn:
        finance_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO memory_domains (id, slug, name, created_at, updated_at) "
                "VALUES (:id, 'finance', 'Finance', now(), now())"
            ),
            {"id": finance_id},
        )
        _insert_legacy_item(conn, id="ki_legacy_caps", domain="Finance")
        # Mixed case *and* a separator run, so the two halves of the formula
        # are exercised together rather than one masking the other.
        _insert_legacy_item(conn, id="ki_legacy_caps_multiword", domain="Q-Team Notes")

    command.upgrade(cfg, "0062_knowledge_domains_backfill")

    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT domain_id FROM knowledge_item_domains WHERE item_id = 'ki_legacy_caps'")
        ).fetchall()
        assert [r[0] for r in rows] == [finance_id], (
            "a capitalised legacy domain must land on the existing 'finance' domain, not a new one"
        )

        junk = conn.execute(
            sa.text("SELECT slug FROM memory_domains WHERE slug LIKE '-%' OR slug <> lower(slug)")
        ).fetchall()
        assert junk == [], f"backfill minted junk domain slugs: {[r[0] for r in junk]}"

        multiword = conn.execute(
            sa.text(
                "SELECT md.slug FROM knowledge_item_domains kid "
                "JOIN memory_domains md ON md.id = kid.domain_id "
                "WHERE kid.item_id = 'ki_legacy_caps_multiword'"
            )
        ).fetchall()
        assert [r[0] for r in multiword] == ["q-team-notes"]


def test_0062_collapses_legacy_spellings_that_normalize_to_one_slug(pg_engine):
    """Several raw values normalizing to one slug must yield ONE domain.

    Normalization is many-to-one, and the generated ``md_<slug>`` id collapses
    with the slug — so a per-raw-value INSERT would push rows sharing a
    primary key through a single statement, which ``ON CONFLICT (slug)``
    neither arbitrates nor deduplicates within one statement, aborting the
    whole migration on the pkey violation. Both axes are covered here: case
    (``Finance``/``finance``) and separator runs (``q team``/``q-team``).
    """
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0061_agent_status_backfill_v115")

    with pg_engine.begin() as conn:
        for i, value in enumerate(["Finance", "finance", "FINANCE", "q team", "q-team", "Q  Team"]):
            _insert_legacy_item(conn, id=f"ki_variant_{i}", domain=value)

    command.upgrade(cfg, "0062_knowledge_domains_backfill")

    with pg_engine.connect() as conn:
        slugs = sorted(
            r[0] for r in conn.execute(sa.text("SELECT slug FROM memory_domains WHERE slug IN ('finance', 'q-team')"))
        )
        assert slugs == ["finance", "q-team"]

        # Every one of the six items resolves to one of those two domains.
        pairs = conn.execute(
            sa.text(
                "SELECT kid.item_id, md.slug FROM knowledge_item_domains kid "
                "JOIN memory_domains md ON md.id = kid.domain_id "
                "WHERE kid.item_id LIKE 'ki_variant_%'"
            )
        ).fetchall()
        assert sorted(pairs) == [
            ("ki_variant_0", "finance"),
            ("ki_variant_1", "finance"),
            ("ki_variant_2", "finance"),
            ("ki_variant_3", "q-team"),
            ("ki_variant_4", "q-team"),
            ("ki_variant_5", "q-team"),
        ]


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
