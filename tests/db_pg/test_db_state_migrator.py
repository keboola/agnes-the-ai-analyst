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


def test_cancel_sentinel_during_data_copy_step(tmp_path, pg_engine, monkeypatch):
    """Phase 7.4 — sentinel arrives mid-migration (after alembic has run,
    during the data_copy call).  The migrator must observe the sentinel at
    the next step boundary (verify), mark_cancelled, and return 0.

    The existing cancel test in tests/test_db_state_migrator.py pre-creates
    the sentinel BEFORE main() runs (boundary 0).  This test complements it
    by exercising the late-stage boundary check that fires AFTER copy returns.
    """
    import json
    import duckdb
    from src.db import _ensure_schema
    from scripts.db_state_migrator import main, copy_duckdb_to_pg

    duck_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(duck_path))
    _ensure_schema(conn)
    conn.close()

    jobs_dir = tmp_path / "db-jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    job_id = "job-cancel-mid-copy"
    orig_copy = copy_duckdb_to_pg

    def copy_then_drop_sentinel(duck_path, target_url):
        result = orig_copy(duck_path, target_url)
        # Sentinel arrives after copy finishes — the next boundary check
        # (verify) must trip it.
        (jobs_dir / f"{job_id}.cancel").touch()
        return result

    monkeypatch.setattr(
        "scripts.db_state_migrator.copy_duckdb_to_pg",
        copy_then_drop_sentinel,
    )

    rc = main(
        job_id=job_id,
        to="side_car",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0, "cancellation is not a process failure"

    job = json.loads((jobs_dir / f"{job_id}.json").read_text())
    assert job["status"] == "cancelled", f"unexpected status: {job['status']}"
    # The cancel must be observed at the verify boundary (first check after
    # copy returns), NOT inside data_copy itself.
    assert job["error"]["step"] == "verify", (
        f"expected cancel at verify step, got: {job['error']['step']}"
    )


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


# ---------------------------------------------------------------------------
# Phase 7.5 — Hung-migrator statement_timeout contract
# ---------------------------------------------------------------------------

def test_bounded_engine_carries_statement_timeout(pg_engine):
    """Phase 7.5 — _bounded_engine must set PG-side statement_timeout
    to 5 minutes (300_000 ms). This bounds any single query so a
    hung migrator subprocess cannot sit forever on a runaway query;
    the unattended applier needs it to surface a clear error within
    a known horizon. Without this guard, a misconfigured target
    (broken index, deadlock with another client) could block the
    migrator indefinitely.

    This test exercises the runtime behaviour: the PG server
    actually honours the setting when a connection is established
    via _bounded_engine.
    """
    import sqlalchemy as sa
    from scripts.db_state_migrator import _bounded_engine

    eng = _bounded_engine(str(pg_engine.url))
    with eng.connect() as conn:
        row = conn.execute(sa.text("SHOW statement_timeout")).fetchone()
    eng.dispose()

    raw = row[0]

    def parse_ms(v: str) -> int:
        """Normalise PG's SHOW statement_timeout output to integer ms.

        PG may return the value in several formats depending on version and
        unit: "300000ms", "300s", "5min", or bare digits (treated as ms
        by PG's SHOW output — unlike SET which treats bare digits as ms
        only in some contexts). We normalise all to integer ms so the
        assertion is unit-independent.
        """
        v = v.strip()
        if v.endswith("ms"):
            return int(v[:-2])
        if v.endswith("min"):
            return int(v[:-3]) * 60_000
        if v.endswith("s"):
            return int(v[:-1]) * 1_000
        # bare integer — PG SHOW output for this GUC uses ms
        return int(v)

    assert parse_ms(raw) == 300_000, (
        f"statement_timeout {raw!r} != 5 min (300_000 ms) — "
        "_bounded_engine is not enforcing the query time-cap"
    )


def test_bounded_engine_connect_args_carry_statement_timeout():
    """Phase 7.5 — static contract: the literal option string
    ``-c statement_timeout=300000`` must be present in
    ``_bounded_engine``'s source.

    This is a pure static guard: it fails immediately on accidental
    deletion of the option, without needing a PG server. The runtime
    counterpart (``test_bounded_engine_carries_statement_timeout``)
    proves PG actually honours the setting; this test catches
    accidental removal before it can reach CI.
    """
    import inspect
    from scripts.db_state_migrator import _bounded_engine

    src = inspect.getsource(_bounded_engine)
    assert "statement_timeout=300000" in src, (
        "5-minute statement_timeout (300_000 ms) is missing from "
        "_bounded_engine — the hung-migrator guard has been removed"
    )


# ---------------------------------------------------------------------------
# Phase 7.2 — DuckDB → CLOUD direct end-to-end
# ---------------------------------------------------------------------------

def test_main_duckdb_to_cloud_end_to_end(tmp_path, pg_engine, monkeypatch):
    """Phase 7.2 — direct DuckDB → CLOUD path.

    Unlike duckdb→side_car (which runs backup_duckdb pre-copy), the
    direct-cloud path SKIPS backup_duckdb because the operator went straight
    to managed PG. This test locks in that distinction:

      - rc=0 and status=success
      - state machine flipped to CLOUD with the target URL stored
      - user row present in PG
      - NO duckdb backup file produced (the duckdb→side_car backup sentinel
        ``duckdb-pre-sidecar-*.duckdb.gz`` must not appear)
    """
    import json
    import duckdb
    import sqlalchemy as sa
    from src.db import _ensure_schema
    from scripts.db_state_migrator import main

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
    write_backend_state(BackendState.CLOUD_IN_PROGRESS)

    rc = main(
        job_id="job-cloud-direct",
        to="cloud",
        source_backend="duckdb",
        target_url=str(pg_engine.url),
        duckdb_path=duck_path,
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0, "duckdb→cloud direct migration must complete with rc=0"

    job = json.loads((jobs_dir / "job-cloud-direct.json").read_text())
    assert job["status"] == "success", f"unexpected status: {job}"

    # State flipped to CLOUD with the target URL.
    state, url = read_backend_state()
    assert state == BackendState.CLOUD
    assert url == str(pg_engine.url)

    # Data copied across.
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT email FROM users WHERE id = :id"), {"id": "u1"}
        ).fetchone()
    assert row is not None
    assert row[0] == "a@x"

    # Contract guard: direct-cloud must NOT have produced a duckdb backup.
    # The duckdb→side_car path produces a ``duckdb-pre-sidecar-*.duckdb.gz``
    # file; duckdb→cloud does not (no backup_duckdb call in that branch).
    backups = list(backups_dir.glob("*.duckdb.gz")) if backups_dir.exists() else []
    assert backups == [], f"direct-cloud must skip duckdb backup, found {backups}"


# ---------------------------------------------------------------------------
# Phase 7.3 — CLOUD → SIDE_CAR DR rollback (smoke variant)
# ---------------------------------------------------------------------------

def test_main_cloud_to_side_car_dr_rollback_smoke(tmp_path, pg_engine, monkeypatch):
    """Phase 7.3 — CLOUD → SIDE_CAR DR rollback.

    The realistic test requires two PG instances (source + target on different
    hosts). Without a second pgserver fixture we run a smoke variant: the same
    engine URL is used for both source and target, relying on copy_pg_to_pg's
    idempotency (which is already covered by
    ``test_copy_pg_to_pg_idempotent_same_url``). This exercises the state-machine
    dispatch, flow control, verify step, and flip end-to-end.

    Limitation documented: the full cross-host case requires a second PG
    server and is validated by live testing on agnes-dev with a Cloud SQL
    source. This test guards the state-machine logic and API contract only.
    """
    import json
    import sqlalchemy as sa
    from src.db_pg import Base
    from scripts.db_state_migrator import main, alembic_upgrade_head

    # Set up: source PG fully migrated + has a row, treated as 'cloud'.
    alembic_upgrade_head(str(pg_engine.url))
    with pg_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO users (id, email, name) VALUES ('u-dr', 'dr@example.com', 'DR')"
        ))

    jobs_dir = tmp_path / "db-jobs"
    backups_dir = tmp_path / "backups"
    overlay = tmp_path / "instance.yaml"
    monkeypatch.setattr("src.db_state_machine._OVERLAY_PATH", overlay)

    from src.db_state_machine import BackendState, write_backend_state, read_backend_state
    # The API endpoint writes SIDE_CAR_IN_PROGRESS before spawning the migrator.
    write_backend_state(BackendState.SIDE_CAR_IN_PROGRESS, url=str(pg_engine.url))

    # Source URL == target URL (smoke variant). Real DR would have
    # source = cloud SQL, target = sidecar PG container.
    rc = main(
        job_id="job-cloud-to-sidecar",
        to="side_car",
        source_backend="cloud",
        source_url=str(pg_engine.url),
        target_url=str(pg_engine.url),
        duckdb_path=tmp_path / "unused.duckdb",
        jobs_dir=jobs_dir,
        backups_dir=backups_dir,
    )
    assert rc == 0, "cloud→side_car PG→PG migration must complete with rc=0"

    job = json.loads((jobs_dir / "job-cloud-to-sidecar.json").read_text())
    assert job["status"] == "success", f"unexpected status: {job}"

    state, url = read_backend_state()
    assert state == BackendState.SIDE_CAR
    assert url == str(pg_engine.url)

    # User row still present (idempotent copy).
    with pg_engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT email FROM users WHERE id = :id"), {"id": "u-dr"}
        ).fetchone()
    assert row is not None
    assert row[0] == "dr@example.com"


# ---------------------------------------------------------------------------
# Phase 7.6 — Host-reboot recovery (unit-level, no bash harness)
# ---------------------------------------------------------------------------

def test_stuck_running_recovery_via_stale_heartbeat(tmp_path):
    """Phase 7.6 — host-reboot simulation.

    JobWriter wrote 'running' + heartbeat at T0; host reboots between T0
    and T+200s; alive file mtime is left at T0. The applier's recovery loop
    (scripts/ops/agnes-state-applier.sh) inspects alive mtime and marks the
    job failed when age >120s.

    We don't invoke the bash applier here — that's covered by the shell
    test suite. This test locks in the UNIT-level file contract between
    JobWriter and the applier: files have the right names, JSON has the
    right shape, and the stale-heartbeat predicate (age > 120s) is
    accurately detectable from the on-disk artifacts.

    The test also emulates the applier's recovery write to confirm the
    resulting JSON is what the API endpoint's status reader expects.
    """
    import os, time, json
    from scripts.db_state_migrator import JobWriter

    job_id = "stuck-running-job"
    w = JobWriter(
        job_id=job_id,
        jobs_dir=tmp_path,
        source="duckdb",
        target="side_car",
    )
    w.write_initial()
    w.update_step("data_copy", progress_pct=40)

    # Verify the alive sentinel was produced by write_initial / update_step.
    alive_path = tmp_path / f"{job_id}.alive"
    assert alive_path.exists(), "JobWriter must produce alive sentinel file"

    # Backdate the alive file to 200s ago to simulate the host having
    # rebooted while the migrator was mid-copy.
    two_hundred_s_ago = time.time() - 200
    os.utime(alive_path, (two_hundred_s_ago, two_hundred_s_ago))

    # Confirm the on-disk state matches what the applier looks for:
    # status=running in the JSON, alive mtime stale by >120s.
    job = json.loads((tmp_path / f"{job_id}.json").read_text())
    assert job["status"] == "running", "job must be running before recovery"
    age_s = time.time() - alive_path.stat().st_mtime
    assert age_s > 120, f"backdating failed — age only {age_s:.1f}s"

    # Emulate the applier's recovery write — the same predicate the
    # bash loop uses, ported to Python for test isolation.  This
    # validates that *if* the applier runs this logic, the JSON ends up
    # in the shape the API status reader expects.
    if job.get("status") == "running" and age_s > 120:
        job["status"] = "failed"
        job["error"] = {
            "step": job.get("current_step", "unknown"),
            "class": "StuckRunning",
            "message": f"stuck running (no heartbeat for {int(age_s)}s)",
        }
        (tmp_path / f"{job_id}.json").write_text(json.dumps(job, indent=2))

    # Verify the recovery wrote the expected shape.
    recovered = json.loads((tmp_path / f"{job_id}.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["error"]["class"] == "StuckRunning"
    assert "stuck running" in recovered["error"]["message"]
    # The step recorded in the error must be the step that was active
    # when the host rebooted.
    assert recovered["error"]["step"] == "data_copy"
