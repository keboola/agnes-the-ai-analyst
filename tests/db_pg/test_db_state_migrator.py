"""PG-backed tests for db_state_migrator (alembic upgrade step).

JobWriter unit tests live in ``tests/test_db_state_migrator.py`` because
they don't need a Postgres instance. This module covers steps that
require a real PG target — currently just ``alembic_upgrade_head``.
"""
from __future__ import annotations


def test_alembic_upgrade_head_runs(tmp_path, pg_engine):
    """alembic_upgrade_head brings target to current head revision."""
    from scripts.db_state_migrator import alembic_upgrade_head

    alembic_upgrade_head(str(pg_engine.url))

    # Verify alembic_version row exists with head revision
    import sqlalchemy as sa
    with pg_engine.connect() as conn:
        row = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchone()
    assert row is not None
    assert len(row[0]) > 0


def test_copy_duckdb_to_pg_full_cycle(tmp_path, pg_engine):
    """Seed DuckDB → copy to PG → verify rows present."""
    import duckdb
    from src.db import _ensure_schema

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users (id, email, name) VALUES ('u1', 'alice@example.com', 'Alice')"
    )
    conn.close()

    from scripts.db_state_migrator import alembic_upgrade_head, copy_duckdb_to_pg
    alembic_upgrade_head(str(pg_engine.url))

    summary = copy_duckdb_to_pg(duck_path, str(pg_engine.url))
    assert summary["rows_total"] >= 1

    import sqlalchemy as sa
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT email FROM users WHERE id = :id"), {"id": "u1"}
        ).fetchone()
    assert row[0] == "alice@example.com"


def test_verify_row_counts_match(tmp_path, pg_engine):
    """After copy, source and target row counts match."""
    import duckdb, sqlalchemy as sa
    from src.db import _ensure_schema
    from scripts.db_state_migrator import (
        alembic_upgrade_head,
        copy_duckdb_to_pg,
        verify_row_counts,
    )

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A'), ('u2', 'b@x', 'B')")
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))
    copy_duckdb_to_pg(duck_path, str(pg_engine.url))

    diffs = verify_row_counts(duck_path, str(pg_engine.url))
    # Empty diffs = all tables match
    assert diffs == [], f"Row count diffs: {diffs}"


def test_verify_row_counts_detects_mismatch(tmp_path, pg_engine):
    """When PG missing rows, verify returns table-level diff."""
    import duckdb, sqlalchemy as sa
    from src.db import _ensure_schema
    from scripts.db_state_migrator import alembic_upgrade_head, verify_row_counts

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')")
    conn.close()

    alembic_upgrade_head(str(pg_engine.url))
    # Skip copy — leave PG empty

    diffs = verify_row_counts(duck_path, str(pg_engine.url))
    user_diff = next(d for d in diffs if d["table"] == "users")
    assert user_diff["source_rows"] == 1
    assert user_diff["target_rows"] == 0


def test_main_duckdb_to_side_car_end_to_end(tmp_path, pg_engine, monkeypatch):
    """End-to-end: main(--to=side_car) drives all steps + writes success."""
    import json
    import duckdb
    from src.db import _ensure_schema

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.execute("INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')")
    conn.close()

    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)
    from src.db_state_machine import BackendState, write_backend_state, read_backend_state
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS)

    from scripts.db_state_migrator import main
    rc = main(
        job_id="job-test-1",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0

    job = json.loads((jobs_dir / "job-test-1.json").read_text())
    assert job["status"] == "success"
    assert job["summary"]["tables_migrated"] > 0

    # State machine flipped to stable side_car
    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR
    assert url == str(pg_engine.url)


def test_copy_pg_to_pg_idempotent_same_url(tmp_path, pg_engine):
    """copy_pg_to_pg(url, url) — copying the schema onto itself is a no-op.

    Smoke test guarding the side_car → cloud path; the row-handling
    logic (JSON cast, ARRAY coerce, NOT NULL default sub) is shared
    with the DuckDB path, so the dedicated tests on that path cover
    the per-column edge cases. Real cross-host PG→PG verification
    happens live on agnes-dev with a Cloud SQL target.
    """
    import sqlalchemy as sa
    from src.db_pg import Base
    from scripts.db_state_migrator import copy_pg_to_pg, verify_pg_row_counts

    # Create the empty schema on the test PG.
    Base.metadata.create_all(pg_engine)
    url = str(pg_engine.url)

    # Seed a row in source so we have something non-trivial to copy.
    with pg_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, name) VALUES ('u1', 'a@x', 'A')"
        ))

    summary = copy_pg_to_pg(url, url)
    assert summary["tables_migrated"] > 0
    # Idempotent — re-running yields the same row count.
    summary2 = copy_pg_to_pg(url, url)
    assert summary2["rows_total"] == summary["rows_total"]

    # Verification reports no diffs.
    diffs = verify_pg_row_counts(url, url)
    assert diffs == []


def test_main_to_cloud_requires_source_url(tmp_path, monkeypatch):
    """main(--to=cloud) without source_url raises ValueError.

    The applier passes --source-url explicitly; CLI fallback reads
    instance.yaml. If neither is set, fail loud rather than silently
    re-migrate from DuckDB (the v6 footgun we fixed in v7).
    """
    from scripts.db_state_migrator import main

    # Isolate the state-machine overlay path so the test doesn't read
    # or write to the host-default /data/state/instance.yaml.
    monkeypatch.setattr(
        "src.db_state_machine._OVERLAY_PATH",
        tmp_path / "instance.yaml",
    )

    rc = main(
        job_id="job-cloud-1",
        to="cloud",
        target_url="postgresql+psycopg://x:y@z/q",
        duckdb_path=tmp_path / "system.duckdb",
        jobs_dir=tmp_path / "db-jobs",
        backups_dir=tmp_path / "backups",
        source_url=None,
        source_backend="side_car",
    )
    # main() catches the exception and writes failed status; rc=1.
    assert rc == 1
    import json
    job = json.loads((tmp_path / "db-jobs" / "job-cloud-1.json").read_text())
    assert job["status"] == "failed"
    assert "source-url" in job["error"]["message"].lower()


def test_verify_raises_on_missing_target_table(tmp_path, pg_engine):
    """If a target table is missing (e.g. partial alembic apply),
    verify_row_counts must raise — not return ``tgt_count = 0`` and
    silently match an empty source. Hides typos AND partial schemas."""
    import duckdb
    import pytest
    import src.models  # noqa: F401 — registers all ORM models onto Base.metadata
    from sqlalchemy import text as sa_text
    from src.db import _ensure_schema
    from src.db_pg import Base
    from scripts.db_state_migrator import verify_row_counts

    duck_path = tmp_path / "src.duckdb"
    duck = duckdb.connect(str(duck_path))
    _ensure_schema(duck)
    duck.close()

    Base.metadata.create_all(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(sa_text("DROP TABLE users CASCADE"))

    with pytest.raises(RuntimeError, match="target table.*missing"):
        verify_row_counts(duck_path, str(pg_engine.url))


def test_verify_pg_raises_on_missing_target_table(tmp_path, pg_engine):
    """Same contract for the PG -> PG verify variant (used on
    side_car -> cloud and cloud -> side_car transitions)."""
    import pytest
    import src.models  # noqa: F401 — registers all ORM models onto Base.metadata
    from sqlalchemy import text as sa_text
    from src.db_pg import Base
    from scripts.db_state_migrator import verify_pg_row_counts

    Base.metadata.create_all(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(sa_text("DROP TABLE users CASCADE"))

    # Same URL on both sides — the test exercises the PROGRAMMING
    # error code path (table missing on the target side). The fact
    # that source side ALSO has the table missing is irrelevant; the
    # missing-target raise must fire regardless.
    with pytest.raises(RuntimeError, match="(target|source) table.*missing"):
        verify_pg_row_counts(str(pg_engine.url), str(pg_engine.url))


def test_main_writes_duckdb_backup_before_copy(tmp_path, pg_engine, monkeypatch):
    """The DuckDB backup must exist on disk BEFORE the data_copy step
    overwrites any PG state. The previous flow copied first, verified,
    then backed up — so a crash between verify and flip left the
    operator with neither a backup nor a flipped state."""
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import main

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    # Force a failure AFTER the backup step by patching copy_duckdb_to_pg
    # to raise. If the backup was written before the failure, the file
    # exists on disk.
    def boom(*a, **kw):
        raise RuntimeError("simulated mid-copy crash")
    monkeypatch.setattr("scripts.db_state_migrator.copy_duckdb_to_pg", boom)

    rc = main(
        job_id="job-backup-order",
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 1
    backups = list(backups_dir.glob("duckdb-pre-sidecar-*.duckdb.gz"))
    assert backups, "backup file should exist even though copy failed"


def test_bounded_engine_fails_fast_on_unreachable(tmp_path):
    """A bogus host must error within ~connect_timeout, not hang.
    The test asserts the engine raises within 15s end-to-end —
    plenty of headroom over the 10s connect_timeout."""
    import time, pytest, sqlalchemy as sa
    from scripts.db_state_migrator import _bounded_engine
    eng = _bounded_engine("postgresql+psycopg://x:y@10.255.255.1:5432/nope")
    t0 = time.monotonic()
    with pytest.raises((sa.exc.OperationalError, sa.exc.DBAPIError)):
        with eng.connect() as c:
            c.execute(sa.text("SELECT 1"))
    elapsed = time.monotonic() - t0
    assert elapsed < 15, f"connect_timeout did not fire within 15s, took {elapsed:.1f}s"
