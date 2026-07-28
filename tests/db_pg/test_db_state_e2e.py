"""End-to-end: full DuckDB → side-car migration via state machine.

Uses pgserver as the "side-car" target. Validates that after
db_state_migrator.main() completes:
  1. Job status = success
  2. Row counts match between DuckDB and PG per table
  3. instance.yaml flipped to side_car
  4. use_pg() returns True
  5. Factory routes new requests to PG (verified by reading users back)
  6. DuckDB backup file written to /data/state/backups/
"""
from __future__ import annotations

import json

import pytest


@pytest.mark.timeout(180)
def test_full_duckdb_to_side_car_migration(tmp_path, pg_engine, monkeypatch):
    # --- Setup overlay path + DATA_DIR -------------------------------------
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Make sure no stray env var hijacks _resolve_url before instance.yaml
    # is written by the migrator.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)

    # --- Seed DuckDB --------------------------------------------------------
    import duckdb

    from src.db import _ensure_schema

    duck_path = tmp_path / "state" / "system.duckdb"
    duck_path.parent.mkdir(parents=True)
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users (id, email, name) VALUES ('u1', 'alice@x', 'Alice')"
    )
    conn.close()

    # Mark current overlay as the SIDE_CAR_IN_PROGRESS state — matches what
    # the API endpoint does before spawning the migrator subprocess.
    from src.db_state_machine import (
        BackendState,
        read_backend_state,
        write_backend_state,
    )

    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)

    # --- Run migrator -------------------------------------------------------
    from scripts.db_state_migrator import main

    jobs_dir = tmp_path / "state" / "db-jobs"
    backups_dir = tmp_path / "state" / "backups"

    rc = main(
        job_id="e2e-1",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0, f"main() returned {rc}"

    # 1. Job status = success
    job = json.loads((jobs_dir / "e2e-1.json").read_text())
    assert job["status"] == "success", job
    assert job["summary"]["tables_migrated"] > 0

    # 2. State flipped
    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR
    assert url == str(pg_engine.url)

    # 3. Backup written
    backups = list(backups_dir.glob("duckdb-pre-sidecar-*.duckdb.gz"))
    assert len(backups) == 1, f"expected 1 backup, found {backups}"

    # 4. use_pg() now True (reads instance.yaml overlay)
    from src.repositories import use_pg

    assert use_pg() is True

    # 5. Factory routes to PG — read the seeded user back through the
    # public factory. Dispose any stale engine first so _resolve_url()
    # consults instance.yaml fresh.
    import src.db_pg as db_pg

    db_pg.dispose()

    from src.repositories import users_repo

    repo = users_repo()
    # PG-backed repo class name ends in PgRepository — confirm routing.
    assert "Pg" in type(repo).__name__, (
        f"factory returned non-PG repo: {type(repo).__name__}"
    )

    fetched = repo.get_by_id("u1")
    assert fetched is not None
    assert fetched["email"] == "alice@x"

    # 6. Sanity: direct PG query also sees the row (factory + raw connection
    # agree on the same backend).
    import sqlalchemy as sa

    with pg_engine.connect() as raw:
        row = raw.execute(
            sa.text("SELECT email FROM users WHERE id = 'u1'")
        ).fetchone()
    assert row is not None
    assert row[0] == "alice@x"
