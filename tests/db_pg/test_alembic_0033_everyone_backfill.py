"""Alembic 0033 (DuckDB v86, issue #748) — backfill users missing Everyone.

Mirrors ``tests/test_db.py::TestV85ToV86Migration`` for the Postgres ladder:
env unset backfills users lacking a row in the seeded Everyone group;
env set no-ops (Everyone is Workspace-controlled).
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


def _everyone_group_id(conn) -> str:
    row = conn.execute(sa.text("SELECT id FROM user_groups WHERE name = 'Everyone' AND is_system")).fetchone()
    assert row is not None, "Everyone system group missing after 0003_rbac seed"
    return row[0]


def test_0033_backfills_users_missing_everyone_when_env_unset(pg_engine, monkeypatch):
    from alembic import command

    monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0032_vscode_mcp_client_v85")

    missing_uid = "u-" + uuid.uuid4().hex[:8]
    already_uid = "u-" + uuid.uuid4().hex[:8]

    with pg_engine.begin() as conn:
        # Seed the Admin/Everyone system groups the way 0003_rbac's app-level
        # seed does at boot (Alembic itself doesn't seed them — see the
        # revision's fresh-install no-op comment).
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                "VALUES (:id, 'Everyone', 'System', TRUE, 'system:seed') "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"id": uuid.uuid4().hex},
        )
        everyone_gid = _everyone_group_id(conn)

        for uid, email in [
            (missing_uid, f"{missing_uid}@x"),
            (already_uid, f"{already_uid}@x"),
        ]:
            conn.execute(
                sa.text("INSERT INTO users (id, email, name) VALUES (:id, :email, :email)"),
                {"id": uid, "email": email},
            )
        conn.execute(
            sa.text(
                "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
                "VALUES (:uid, :gid, 'admin', 'admin@x')"
            ),
            {"uid": already_uid, "gid": everyone_gid},
        )

    command.upgrade(cfg, "0033_everyone_backfill_v86")

    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT source FROM user_group_members WHERE user_id = :uid AND group_id = :gid"),
            {"uid": missing_uid, "gid": everyone_gid},
        ).fetchall()
        assert len(rows) == 1, f"expected exactly one backfilled row, got {rows}"
        assert rows[0][0] == "system_seed"

        rows2 = conn.execute(
            sa.text("SELECT source FROM user_group_members WHERE user_id = :uid AND group_id = :gid"),
            {"uid": already_uid, "gid": everyone_gid},
        ).fetchall()
        assert rows2 == [("admin",)], "pre-existing membership must not be duplicated/overwritten"


def test_0033_noops_when_env_set(pg_engine, monkeypatch):
    from alembic import command

    monkeypatch.setenv("AGNES_GROUP_EVERYONE_EMAIL", "everyone@workspace.test")

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0032_vscode_mcp_client_v85")

    missing_uid = "u-" + uuid.uuid4().hex[:8]

    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                "VALUES (:id, 'Everyone', 'System', TRUE, 'system:seed') "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"id": uuid.uuid4().hex},
        )
        everyone_gid = _everyone_group_id(conn)
        conn.execute(
            sa.text("INSERT INTO users (id, email, name) VALUES (:id, :email, :email)"),
            {"id": missing_uid, "email": f"{missing_uid}@x"},
        )

    command.upgrade(cfg, "0033_everyone_backfill_v86")

    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT 1 FROM user_group_members WHERE user_id = :uid AND group_id = :gid"),
            {"uid": missing_uid, "gid": everyone_gid},
        ).fetchall()
        assert rows == [], (
            "backfill must no-op when AGNES_GROUP_EVERYONE_EMAIL is set — Everyone is Workspace-controlled"
        )


def test_0033_noops_gracefully_on_fresh_install_with_no_groups_yet(pg_engine, monkeypatch):
    """Fresh PG install: Alembic runs before the app's first boot seeds the
    system groups, so the Everyone group row doesn't exist yet. The
    migration must not raise."""
    from alembic import command

    monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0032_vscode_mcp_client_v85")

    # No user_groups seeded at all — simulates a fresh install.
    command.upgrade(cfg, "0033_everyone_backfill_v86")  # must not raise

    with pg_engine.connect() as conn:
        count = conn.execute(sa.text("SELECT COUNT(*) FROM user_group_members")).scalar()
    assert count == 0


def test_0033_downgrade_removes_only_backfilled_rows(pg_engine, monkeypatch):
    from alembic import command

    monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, "0032_vscode_mcp_client_v85")

    backfilled_uid = "u-" + uuid.uuid4().hex[:8]
    manual_uid = "u-" + uuid.uuid4().hex[:8]

    with pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                "VALUES (:id, 'Everyone', 'System', TRUE, 'system:seed') "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"id": uuid.uuid4().hex},
        )
        everyone_gid = _everyone_group_id(conn)
        for uid, email in [
            (backfilled_uid, f"{backfilled_uid}@x"),
            (manual_uid, f"{manual_uid}@x"),
        ]:
            conn.execute(
                sa.text("INSERT INTO users (id, email, name) VALUES (:id, :email, :email)"),
                {"id": uid, "email": email},
            )
        conn.execute(
            sa.text(
                "INSERT INTO user_group_members (user_id, group_id, source, added_by) "
                "VALUES (:uid, :gid, 'admin', 'admin@x')"
            ),
            {"uid": manual_uid, "gid": everyone_gid},
        )

    command.upgrade(cfg, "0033_everyone_backfill_v86")
    command.downgrade(cfg, "0032_vscode_mcp_client_v85")

    with pg_engine.connect() as conn:
        backfilled = conn.execute(
            sa.text("SELECT 1 FROM user_group_members WHERE user_id = :uid AND group_id = :gid"),
            {"uid": backfilled_uid, "gid": everyone_gid},
        ).fetchall()
        manual = conn.execute(
            sa.text("SELECT source FROM user_group_members WHERE user_id = :uid AND group_id = :gid"),
            {"uid": manual_uid, "gid": everyone_gid},
        ).fetchall()
    assert backfilled == [], "downgrade must remove the system:v86-backfill row"
    assert manual == [("admin",)], "downgrade must not touch non-backfill rows"
