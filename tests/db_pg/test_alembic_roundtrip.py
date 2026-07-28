"""Round-trip + drift discipline.

These three tests are the load-bearing safety net of the whole migration
chain. They cost ~zero developer effort to maintain (parametrized) and
fire RED on three common, dangerous mistakes:

  - someone adds an upgrade() body without writing a matching downgrade()
  - someone changes a SQLAlchemy model without committing a migration
  - someone commits a migration without updating the model

Per the parent plan, this is the discipline that retires the
``system.duckdb.pre-migrate`` snapshot dance.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from tests.db_pg.snapshot import snapshot_schema


REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def _list_revisions():
    """Return all Alembic revisions in chain order (oldest → newest)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    # walk_revisions emits newest → oldest; reverse for natural order.
    return list(reversed(list(script.walk_revisions())))


def test_full_chain_roundtrip(pg_engine):
    """upgrade head → snapshot → downgrade base → upgrade head → snapshot.

    The two snapshots must be IDENTICAL. If any migration's downgrade()
    leaves residue (e.g. dropped a column but forgot the index), this
    test fires red.
    """
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))

    command.upgrade(cfg, "head")
    snap_a = snapshot_schema(pg_engine)

    command.downgrade(cfg, "base")

    # After full downgrade, the only acceptable table is alembic_version
    # (the version tracker itself). Snapshot excludes it.
    inspector = sa.inspect(pg_engine)
    leftover = [
        t for t in inspector.get_table_names(schema="public")
        if t != "alembic_version" and not t.startswith("pg_")
    ]
    assert leftover == [], f"downgrade base left tables: {leftover}"

    command.upgrade(cfg, "head")
    snap_b = snapshot_schema(pg_engine)

    assert snap_a == snap_b, (
        "schema after upgrade→downgrade→upgrade differs from clean upgrade; "
        "at least one revision's downgrade is not the true inverse of upgrade"
    )


@pytest.mark.parametrize("revision_id", [r.revision for r in _list_revisions()])
def test_pairwise_roundtrip(pg_engine, revision_id):
    """For every revision N: upgrade to N, snapshot, upgrade to N+1,
    downgrade to N, snapshot — should match.

    Catches "single-step" downgrade failures that the full-chain test
    might miss when later upgrades happen to mask earlier botches.

    Skipped for the head revision (no N+1).
    """
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg_for_listing = Config(str(REPO_ROOT / "alembic.ini"))
    cfg_for_listing.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg_for_listing)

    next_rev = None
    for r in script.walk_revisions():
        if r.down_revision == revision_id:
            next_rev = r.revision
            break

    if next_rev is None:
        pytest.skip(f"{revision_id} is head; no N+1 to pair with")

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, revision_id)
    snap_at_n = snapshot_schema(pg_engine)
    command.upgrade(cfg, next_rev)
    command.downgrade(cfg, revision_id)
    snap_after = snapshot_schema(pg_engine)
    assert snap_at_n == snap_after, (
        f"downgrade {next_rev} → {revision_id} did not restore the schema; "
        f"diff between snapshots indicates a botched downgrade body"
    )


def test_no_model_migration_drift(pg_engine):
    """Run upgrade head, then ask Alembic to autogenerate a diff against
    the live ``Base.metadata``. The diff must be EMPTY.

    Failure modes this catches:
      - model added a column, no migration written → diff has add_column
      - migration changed a type, model didn't update → diff has type change
      - index renamed in only one place → diff has remove+add index

    The test imports ``src.db_pg`` lazily so the suite still runs before
    that module exists (it'll just see an empty metadata and pass
    vacuously; the contract repo tests in later phases will fail loudly
    if the models aren't wired).
    """
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "head")

    try:
        from src.db_pg import Base
    except ImportError:
        pytest.skip("src.db_pg not present yet; drift test waits for Phase D")
        return

    with pg_engine.connect() as conn:
        mc = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_schemas": False,
            },
        )
        diff = compare_metadata(mc, Base.metadata)

    # Filter out alembic's own version tracking table — it's never in
    # Base.metadata and would always show as "extra".
    diff_real = [d for d in diff if not _is_alembic_table_diff(d)]
    assert diff_real == [], (
        "model vs migration drift detected:\n"
        + "\n".join(f"  - {d}" for d in diff_real)
        + "\nFix: either run `alembic revision --autogenerate -m '...'` or "
          "update src/db_pg.py to match the migration."
    )


def _is_alembic_table_diff(diff_entry) -> bool:
    """alembic_version is metadata-internal; ignore it in drift comparisons."""
    if isinstance(diff_entry, tuple) and len(diff_entry) >= 2:
        op = diff_entry[0]
        if op in {"remove_table", "add_table"}:
            tbl = diff_entry[1]
            name = getattr(tbl, "name", None) or (tbl if isinstance(tbl, str) else "")
            return name == "alembic_version"
    return False
