"""Cross-engine contract test for the v116 ``table_registry`` access-policy
columns (#access-policies Task 2).

Asserts the five columns exist on BOTH backends and that ``set_access_policy``
/ ``set_policy_mapping`` round-trip through ``get()`` / ``list_all()`` with
identical observable behavior:
  - DuckDB: built by ``_ensure_schema`` (fresh-install DDL + v115->v116 ladder).
  - Postgres: built by ``alembic upgrade head`` (migration 0062).
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

_POLICY_SQL = "SELECT * FROM t1 WHERE owner = $user_email"

_COLUMNS = (
    "access_policy_sql",
    "access_policy_note",
    "access_policy_updated_at",
    "access_policy_updated_by",
    "policy_mapping",
)


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.table_registry import TableRegistryRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return TableRegistryRepository(conn), conn


def _make_pg_repo(pg_engine):
    from alembic import command
    from alembic.config import Config
    from src.repositories.table_registry_pg import TableRegistryPgRepository

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    return TableRegistryPgRepository(pg_engine)


def test_access_policy_columns_duckdb(tmp_path):
    repo, conn = _make_duckdb_repo(tmp_path)
    try:
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'table_registry'"
            ).fetchall()
        }
        for col in _COLUMNS:
            assert col in cols, f"DuckDB table_registry missing {col}"

        # A freshly registered row carries no policy.
        repo.register(id="t1", name="t1", source_type="keboola")
        row = repo.get("t1")
        assert row is not None
        assert row["access_policy_sql"] is None
        assert row["access_policy_note"] is None
        assert row["access_policy_updated_at"] is None
        assert row["access_policy_updated_by"] is None
        assert bool(row["policy_mapping"]) is False

        repo.set_access_policy("t1", sql=_POLICY_SQL, note="restrict to owner", updated_by="admin@example.com")
        row = repo.get("t1")
        assert row["access_policy_sql"] == _POLICY_SQL
        assert row["access_policy_note"] == "restrict to owner"
        assert row["access_policy_updated_by"] == "admin@example.com"
        assert row["access_policy_updated_at"] is not None

        # list_all() carries the same five columns.
        listed = {r["id"]: r for r in repo.list_all()}
        assert listed["t1"]["access_policy_sql"] == _POLICY_SQL
        assert listed["t1"]["access_policy_note"] == "restrict to owner"

        # sql=None clears all four access_policy_* columns.
        repo.set_access_policy("t1", sql=None, note=None, updated_by="admin@example.com")
        row = repo.get("t1")
        assert row["access_policy_sql"] is None
        assert row["access_policy_note"] is None
        assert row["access_policy_updated_at"] is None
        assert row["access_policy_updated_by"] is None

        # policy_mapping toggles independently of the policy columns.
        repo.set_policy_mapping("t1", True)
        assert bool(repo.get("t1")["policy_mapping"]) is True
        repo.set_policy_mapping("t1", False)
        assert bool(repo.get("t1")["policy_mapping"]) is False
    finally:
        conn.close()


def test_access_policy_columns_pg(pg_engine):
    repo = _make_pg_repo(pg_engine)

    import sqlalchemy as sa

    inspector = sa.inspect(pg_engine)
    cols = {c["name"] for c in inspector.get_columns("table_registry")}
    for col in _COLUMNS:
        assert col in cols, f"Postgres table_registry missing {col}"

    repo.register(id="t1", name="t1", source_type="keboola")
    row = repo.get("t1")
    assert row is not None
    assert row["access_policy_sql"] is None
    assert row["access_policy_note"] is None
    assert row["access_policy_updated_at"] is None
    assert row["access_policy_updated_by"] is None
    assert bool(row["policy_mapping"]) is False

    repo.set_access_policy("t1", sql=_POLICY_SQL, note="restrict to owner", updated_by="admin@example.com")
    row = repo.get("t1")
    assert row["access_policy_sql"] == _POLICY_SQL
    assert row["access_policy_note"] == "restrict to owner"
    assert row["access_policy_updated_by"] == "admin@example.com"
    assert row["access_policy_updated_at"] is not None

    listed = {r["id"]: r for r in repo.list_all()}
    assert listed["t1"]["access_policy_sql"] == _POLICY_SQL
    assert listed["t1"]["access_policy_note"] == "restrict to owner"

    repo.set_access_policy("t1", sql=None, note=None, updated_by="admin@example.com")
    row = repo.get("t1")
    assert row["access_policy_sql"] is None
    assert row["access_policy_note"] is None
    assert row["access_policy_updated_at"] is None
    assert row["access_policy_updated_by"] is None

    repo.set_policy_mapping("t1", True)
    assert bool(repo.get("t1")["policy_mapping"]) is True
    repo.set_policy_mapping("t1", False)
    assert bool(repo.get("t1")["policy_mapping"]) is False
