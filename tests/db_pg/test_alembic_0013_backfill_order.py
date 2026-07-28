"""B5-NEW — alembic 0013 upgrades cleanly on an instance with
pre-existing typed grants.

Pre-fix the migration ordered:
  1. ADD COLUMN (5 per-type nullable FK columns)
  2. CREATE FOREIGN KEY (per type)
  3. ADD CONSTRAINT … CHECK  ← fires on any existing row while typed columns are NULL
  4. UPDATE backfill          ← never reached; alembic already aborted in step 3

The fix reorders to:
  1. ADD COLUMN
  2. UPDATE backfill          ← typed columns populated BEFORE CHECK fires
  3. ADD CONSTRAINT … CHECK   ← every row now satisfies the constraint
  4. CREATE FOREIGN KEY
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


def test_alembic_0013_upgrades_with_existing_typed_grants(pg_engine):
    """Seed a typical pre-0013 row in resource_grants, then run 0013.

    Pre-B5-NEW the CHECK constraint validates BEFORE backfill →
    alembic aborts with IntegrityError on any prod instance with typed grants.

    The test seeds a real table_registry row so that the FK added by 0013
    can point at it — matching the prod shape where grants reference real
    registered tables.
    """
    from alembic import command

    # Bring the DB up to 0012 (the revision before 0013).
    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0012_duckdb_v59_parity")

    table_id = "tbl-" + uuid.uuid4().hex[:8]

    with pg_engine.begin() as conn:
        # Seed the parent table_registry row so the FK can resolve after
        # the backfill sets resource_id_table = resource_id.
        conn.execute(
            sa.text(
                "INSERT INTO table_registry (id, name) "
                "VALUES (:tid, 'sessions') "
                "ON CONFLICT DO NOTHING"
            ),
            {"tid": table_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name) "
                "VALUES (:gid, 'finance') "
                "ON CONFLICT DO NOTHING"
            ),
            {"gid": "grp-test-1"},
        )
        # Pre-seed a resource_grants row in the pre-0013 v59 shape:
        #   resource_type = 'table', resource_id = <table_id>
        # At this point the 5 per-type FK columns don't exist yet
        # (0013 hasn't run), so we only supply the pre-0013 columns.
        conn.execute(
            sa.text(
                "INSERT INTO resource_grants (id, group_id, resource_type, resource_id) "
                "VALUES (:id, :gid, 'table', :tid) "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": uuid.uuid4().hex, "gid": "grp-test-1", "tid": table_id},
        )

    # Now upgrade to 0013.  Pre-B5-NEW this raises IntegrityError
    # (CHECK constraint fires on rows whose resource_id_table is still NULL).
    command.upgrade(cfg, "0013_resource_grants_per_type_fk")

    # The original row survives + the typed FK column is backfilled.
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT resource_id_table FROM resource_grants "
                "WHERE group_id = 'grp-test-1' "
                "AND resource_type = 'table'"
            )
        ).fetchone()

    assert row is not None, "0013 dropped the grant row!"
    assert row.resource_id_table == table_id, (
        f"Expected resource_id_table={table_id!r}, got {row.resource_id_table!r}"
    )


def test_alembic_0013_backfills_all_typed_columns(pg_engine):
    """Verify that all five typed resource types are backfilled correctly.

    Seeds one grant per typed ResourceType (with the corresponding parent row
    in each domain table), upgrades from 0012 to 0013, and checks that every
    per-type column carries the original resource_id.
    """
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0012_duckdb_v59_parity")

    # Use unique IDs to avoid collisions between parameterised test runs.
    sfx = uuid.uuid4().hex[:8]
    tbl_id  = f"tbl-{sfx}"
    pkg_id  = f"pkg-{sfx}"
    md_id   = f"md-{sfx}"
    ki_id   = f"ki-{sfx}"
    rec_id  = f"rec-{sfx}"

    typed_grants = [
        ("grp-t1", "table",          tbl_id),
        ("grp-t2", "data_package",   pkg_id),
        ("grp-t3", "memory_domain",  md_id),
        ("grp-t4", "memory_item",    ki_id),
        ("grp-t5", "recipe",         rec_id),
    ]

    with pg_engine.begin() as conn:
        # Seed parent domain rows so the FK constraints added in Step 4
        # of the migration can resolve after backfill.
        conn.execute(sa.text(
            "INSERT INTO table_registry (id, name) VALUES (:id, 'T') ON CONFLICT DO NOTHING"
        ), {"id": tbl_id})
        conn.execute(sa.text(
            "INSERT INTO data_packages (id, slug, name) VALUES (:id, :slug, 'P') ON CONFLICT DO NOTHING"
        ), {"id": pkg_id, "slug": f"pkg-slug-{sfx}"})
        conn.execute(sa.text(
            "INSERT INTO memory_domains (id, slug, name) VALUES (:id, :slug, 'MD') ON CONFLICT DO NOTHING"
        ), {"id": md_id, "slug": f"md-slug-{sfx}"})
        conn.execute(sa.text(
            "INSERT INTO knowledge_items (id, title) VALUES (:id, 'KI') ON CONFLICT DO NOTHING"
        ), {"id": ki_id})
        conn.execute(sa.text(
            "INSERT INTO recipes (id, slug, title) VALUES (:id, :slug, 'R') ON CONFLICT DO NOTHING"
        ), {"id": rec_id, "slug": f"rec-slug-{sfx}"})

        # Seed one group per grant.
        for gid, _rtype, _rid in typed_grants:
            conn.execute(
                sa.text(
                    "INSERT INTO user_groups (id, name) "
                    "VALUES (:gid, :name) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"gid": gid, "name": gid},
            )
        # Seed resource_grants in the pre-0013 shape (no typed FK columns yet).
        for gid, rtype, rid in typed_grants:
            conn.execute(
                sa.text(
                    "INSERT INTO resource_grants (id, group_id, resource_type, resource_id) "
                    "VALUES (:id, :gid, :rtype, :rid) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"id": uuid.uuid4().hex, "gid": gid, "rtype": rtype, "rid": rid},
            )

    # Upgrade must not raise (CHECK violation or FK violation).
    command.upgrade(cfg, "0013_resource_grants_per_type_fk")

    # Each typed column must carry the original resource_id value.
    checks = [
        ("grp-t1", "resource_id_table",          tbl_id),
        ("grp-t2", "resource_id_data_package",   pkg_id),
        ("grp-t3", "resource_id_memory_domain",  md_id),
        ("grp-t4", "resource_id_memory_item",    ki_id),
        ("grp-t5", "resource_id_recipe",         rec_id),
    ]
    with pg_engine.connect() as conn:
        for gid, col, expected in checks:
            row = conn.execute(
                sa.text(
                    f"SELECT {col} FROM resource_grants WHERE group_id = :gid"
                ),
                {"gid": gid},
            ).fetchone()
            assert row is not None, f"grant for {gid!r} disappeared after upgrade!"
            actual = getattr(row, col)
            assert actual == expected, (
                f"After 0013 backfill: expected {col}={expected!r}, got {actual!r}"
            )


def test_alembic_0013_marketplace_plugin_rows_survive(pg_engine):
    """marketplace_plugin grants (no typed FK column) pass the CHECK constraint.

    These rows must remain with all five per-type columns NULL because
    marketplace_plugin is the sixth ResourceType that uses the legacy
    resource_id path.  The CHECK has an explicit NOT-IN branch for them.
    """
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0012_duckdb_v59_parity")

    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name) "
                "VALUES ('grp-mp', 'marketplace') ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO resource_grants (id, group_id, resource_type, resource_id) "
                "VALUES (:id, 'grp-mp', 'marketplace_plugin', 'acme/my-plugin') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": uuid.uuid4().hex},
        )

    command.upgrade(cfg, "0013_resource_grants_per_type_fk")

    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT resource_id, resource_id_table, resource_id_data_package, "
                "resource_id_memory_domain, resource_id_memory_item, resource_id_recipe "
                "FROM resource_grants WHERE group_id = 'grp-mp'"
            )
        ).fetchone()

    assert row is not None, "marketplace_plugin grant was dropped!"
    assert row.resource_id == "acme/my-plugin"
    # All typed FK columns must remain NULL for marketplace_plugin.
    assert row.resource_id_table is None
    assert row.resource_id_data_package is None
    assert row.resource_id_memory_domain is None
    assert row.resource_id_memory_item is None
    assert row.resource_id_recipe is None
