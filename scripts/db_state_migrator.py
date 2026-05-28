"""Migration subprocess orchestrator for DB backend state machine.

Invoked by app/api/db_state.py as a child subprocess; writes job
status to /data/state/db-jobs/<job_id>.json so the API endpoint can
poll. Steps for duckdb → side_car:

  1. validate connectivity
  2. alembic upgrade head on target
  3. backup DuckDB snapshot (recovery point BEFORE any destructive write)
  4. data copy DuckDB → target (reuses scripts/migrate_duckdb_to_pg)
  5. verify row counts
  6. flip instance.yaml::database

For side_car → cloud, source is the side-car PG; data step uses the
same migrator with a different source connection.

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""
from __future__ import annotations
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


def _bounded_engine(url: str):
    """Return a SQLAlchemy engine with conservative network + query
    timeouts. The migrator runs unattended via the host applier; an
    unreachable target (DNS, firewall, dead SQL Proxy) must NOT hang
    indefinitely. ``connect_timeout`` covers the initial handshake;
    ``statement_timeout`` (PG-side) caps any single query at 5 min,
    enough for the heaviest tables in the current schema but short
    enough to surface a runaway as a clear error.
    """
    import sqlalchemy as sa
    return sa.create_engine(
        url,
        connect_args={
            "connect_timeout": 10,
            "options": "-c statement_timeout=300000",  # 5 min in ms
        },
        pool_pre_ping=True,
        pool_recycle=1800,
    )


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobCancelled(RuntimeError):
    """Raised when the cancel sentinel is observed at a step boundary."""

    def __init__(self, step: str):
        super().__init__(f"Job cancelled at step={step}")
        self.step = step


@dataclass
class JobWriter:
    """Writes /data/state/db-jobs/<job_id>.json on each step transition.

    Atomic write via tmp + os.replace. Schema versioned at 1.
    """

    job_id: str
    jobs_dir: Path
    source: str
    target: str
    _started_at: str = field(default_factory=lambda: _utcnow_iso())

    @property
    def _path(self) -> Path:
        return self.jobs_dir / f"{self.job_id}.json"

    @property
    def cancel_path(self) -> Path:
        """Side-car sentinel file touched by the API cancel endpoint.

        The migrator subprocess polls this at step boundaries (B2).
        We use a separate file rather than a status-flag inside the
        job JSON so the API endpoint can signal cancellation without
        racing the migrator's own writes to the same file.
        """
        return self.jobs_dir / f"{self.job_id}.cancel"

    def check_cancel_requested(self) -> bool:
        """Return True if the cancel sentinel exists.

        Cooperative cancellation: the migrator calls this at every
        step boundary in :func:`main` and raises :class:`JobCancelled`
        when it observes the sentinel. See B2 in the v9 plan.
        """
        return self.cancel_path.exists()

    def _write(self, data: dict[str, Any]) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self._path)

    def _read(self) -> dict[str, Any]:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def write_initial(self) -> None:
        data = {
            "schema_version": 1,
            "job_id": self.job_id,
            "status": JobStatus.RUNNING.value,
            "source_backend": self.source,
            "target_backend": self.target,
            "started_at": self._started_at,
            "completed_at": None,
            "current_step": "validate",
            "progress_pct": 0,
            "summary": None,
            "error": None,
        }
        self._write(data)

    def update_step(self, step: str, *, progress_pct: int) -> None:
        data = self._read()
        data["current_step"] = step
        data["progress_pct"] = progress_pct
        self._write(data)

    def update_table_progress(self, current_table: str, tables_done: int, tables_total: int) -> None:
        data = self._read()
        data["current_step"] = "data_copy"
        data["table_progress"] = {
            "current_table": current_table,
            "tables_done": tables_done,
            "tables_total": tables_total,
        }
        data["progress_pct"] = int(40 + (tables_done / tables_total) * 40)  # 40-80% range
        self._write(data)

    def mark_success(self, summary: dict[str, Any]) -> None:
        data = self._read()
        data["status"] = JobStatus.SUCCESS.value
        data["completed_at"] = _utcnow_iso()
        data["progress_pct"] = 100
        data["summary"] = summary
        self._write(data)

    def mark_failed(self, *, step: str, error_class: str, error_message: str) -> None:
        data = self._read()
        data["status"] = JobStatus.FAILED.value
        data["completed_at"] = _utcnow_iso()
        data["error"] = {
            "step": step,
            "class": error_class,
            "message": error_message,
        }
        self._write(data)

    def mark_cancelled(self, *, step: str) -> None:
        data = self._read()
        data["status"] = JobStatus.CANCELLED.value
        data["completed_at"] = _utcnow_iso()
        data["error"] = {"step": step, "class": "Cancelled", "message": "Admin cancelled migration"}
        self._write(data)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def alembic_upgrade_head(target_url: str) -> None:
    """Run ``alembic upgrade head`` against ``target_url``.

    Idempotent — alembic itself is a no-op when already at head.
    Raises RuntimeError on failure.
    """
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": target_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(repo_root / "alembic.ini"), "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_root),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def copy_duckdb_to_pg(duckdb_path: Path, target_url: str) -> dict[str, int]:
    """Copy all PG-mapped tables from DuckDB to target PG.

    Wraps :func:`scripts.migrate_duckdb_to_pg.run_all` — the same
    idempotent copy loop that the docker-compose data-migrate one-shot
    uses. Returns ``{rows_total, tables_migrated}`` where ``rows_total``
    is the sum of PG row counts across all migrated tables (per the
    validator report — ``ON CONFLICT DO NOTHING`` makes per-task
    rows-inserted untrustworthy, so we use post-copy PG counts).
    """
    import duckdb
    import sqlalchemy as sa

    from scripts.migrate_duckdb_to_pg import run_all

    duck_conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        pg_engine = _bounded_engine(target_url)
        try:
            reports = run_all(duck_conn, pg_engine, validate=True)
        finally:
            pg_engine.dispose()
    finally:
        duck_conn.close()

    ok = [r for r in reports if "error" not in r]
    err = [r for r in reports if "error" in r]
    return {
        "rows_total": sum(r.get("pg_rows", 0) for r in ok),
        "tables_migrated": len(ok),
        "tables_failed": [
            {"table": r["table"], "error": str(r["error"])}
            for r in err
        ],
    }


def _jsonb_columns_for(table_name: str) -> set[str]:
    """Return PG JSONB column names on ``table_name`` from Base.metadata.

    Used by :func:`copy_pg_to_pg` because PG→PG copy reads JSONB columns
    decoded (e.g. a stored JSON string ``"./foo"`` returns Python str
    ``./foo``); the target's ``CAST AS JSONB`` then fails on bare values.
    We re-encode every JSONB value with ``json.dumps`` on the copy path.
    """
    import src.models  # noqa: F401
    from sqlalchemy.dialects.postgresql import JSONB
    from src.db_pg import Base

    table = Base.metadata.tables.get(table_name)
    if table is None:
        return set()
    return {c.name for c in table.columns if isinstance(c.type, JSONB)}


def _json_dumps_for_jsonb(value):
    """JSON-encode ``value`` for a CAST AS JSONB bind.

    ``None`` and already-string-encoded JSON values pass through after
    encode — ``json.dumps`` always emits valid JSON, so the cast on the
    PG side is now guaranteed parseable. The DuckDB path's
    ``_normalize_for_pg`` does the same dict/list encoding but leaves
    strings alone (DuckDB stores JSON as already-encoded strings); the
    PG source returns decoded values, so we re-encode here.
    """
    import json as _json
    if value is None:
        return None
    return _json.dumps(value)


def copy_pg_to_pg(source_url: str, target_url: str) -> dict[str, int]:
    """Copy all PG-mapped tables from one PG to another in FK order.

    Used for ``side_car → cloud`` migration. Mirrors
    :func:`copy_duckdb_to_pg` but with a PG source instead of DuckDB.
    Reuses the same JSON / ARRAY / NOT-NULL-default coercion the DuckDB
    path uses — the column-introspection helpers in
    ``scripts.migrate_duckdb_to_pg.tasks`` work for any source.

    Returns ``{rows_total, tables_migrated}`` based on post-copy target
    row counts (ON CONFLICT DO NOTHING makes per-task inserted-count
    untrustworthy).
    """
    import sqlalchemy as sa

    import src.models  # noqa: F401 — ensures every model is imported
    from src.db_pg import Base
    from scripts.migrate_duckdb_to_pg import _PK_COLUMNS
    from scripts.migrate_duckdb_to_pg.tasks import (
        _array_columns_for,
        _build_insert,
        _coerce_array_value,
        _normalize_for_pg,
        _not_null_columns_with_default,
        _substitute_default,
    )

    source = _bounded_engine(source_url)
    target = _bounded_engine(target_url)
    rows_total = 0
    tables_migrated = 0
    try:
        for table in Base.metadata.sorted_tables:
            tname = table.name
            cols = [c.name for c in table.columns]
            pk_cols = _PK_COLUMNS.get(tname, ["id"])
            array_cols = _array_columns_for(tname)
            default_cols = _not_null_columns_with_default(tname)
            jsonb_cols = _jsonb_columns_for(tname)

            with source.connect() as src_conn:
                rows = src_conn.execute(
                    sa.text(f'SELECT {", ".join(cols)} FROM "{tname}"')
                ).all()
            if not rows:
                tables_migrated += 1
                continue

            insert_sql = _build_insert(tname, cols, pk_cols)
            batch = []
            for r in rows:
                d = {}
                for k, v in zip(cols, r):
                    if k in array_cols:
                        d[k] = _coerce_array_value(v)
                    elif k in jsonb_cols:
                        # Source is PG JSONB → SQLAlchemy returns dict /
                        # list / str / int / bool depending on the stored
                        # value. CAST AS JSONB expects a JSON-encoded
                        # string for ALL of those — including bare
                        # strings (CAST('hello' AS JSONB) fails; CAST
                        # ('"hello"' AS JSONB) succeeds). json.dumps any
                        # non-None value.
                        d[k] = _json_dumps_for_jsonb(v)
                    else:
                        d[k] = _normalize_for_pg(v)
                    if k in default_cols:
                        d[k] = _substitute_default(d[k], default_cols[k])
                batch.append(d)

            with target.begin() as tgt_conn:
                tgt_conn.execute(sa.text(insert_sql), batch)

            with target.connect() as tgt_conn:
                count = tgt_conn.execute(
                    sa.text(f'SELECT COUNT(*) FROM "{tname}"')
                ).scalar()
            rows_total += int(count or 0)
            tables_migrated += 1
    finally:
        source.dispose()
        target.dispose()

    # tables_failed is always empty here: copy_pg_to_pg raises on the
    # first per-table error rather than collecting them (unlike the
    # DuckDB path which uses run_all). Included for API shape parity with
    # copy_duckdb_to_pg so main() can apply the same guard uniformly.
    return {"rows_total": rows_total, "tables_migrated": tables_migrated, "tables_failed": []}


def verify_pg_row_counts(source_url: str, target_url: str) -> list[dict]:
    """Compare PG row counts between source and target.

    Mirrors :func:`verify_row_counts` but reads both sides as PG.
    Returns list of diffs ``[{table, source_rows, target_rows}]``.
    """
    import sqlalchemy as sa
    from src.db_pg import Base

    diffs: list[dict] = []
    source = _bounded_engine(source_url)
    target = _bounded_engine(target_url)
    try:
        for table in Base.metadata.sorted_tables:
            tname = table.name
            try:
                with source.connect() as c:
                    src_count = c.execute(
                        sa.text(f'SELECT COUNT(*) FROM "{tname}"')
                    ).scalar()
            except sa.exc.ProgrammingError as exc:
                # Was: silent src_count=0. Hard-fail so the operator can act —
                # a missing source table means the schema is broken and migration
                # cannot be validated safely.
                raise RuntimeError(
                    f"verify_pg_row_counts: source table '{tname}' is missing from PG "
                    f"(or the connection lacks SELECT on it). Migration cannot "
                    f"complete safely. Underlying error: {exc!s}"
                ) from exc
            try:
                with target.connect() as c:
                    tgt_count = c.execute(
                        sa.text(f'SELECT COUNT(*) FROM "{tname}"')
                    ).scalar()
            except sa.exc.ProgrammingError as exc:
                # Was: silent tgt_count=0. That collapsed to a 0==0 "match"
                # whenever source also had 0 rows, hiding typos and partial-
                # alembic states. Hard-fail with a specific message so the
                # operator can act.
                raise RuntimeError(
                    f"verify_pg_row_counts: target table '{tname}' is missing from PG "
                    f"(or the connection lacks SELECT on it). Migration cannot "
                    f"complete safely. Underlying error: {exc!s}"
                ) from exc
            if src_count != tgt_count:
                diffs.append({
                    "table": tname,
                    "source_rows": int(src_count or 0),
                    "target_rows": int(tgt_count or 0),
                })
    finally:
        source.dispose()
        target.dispose()
    return diffs


def verify_row_counts(duckdb_path: Path, target_url: str) -> list[dict]:
    """Compare row counts per Base.metadata table between DuckDB and PG.

    Returns list of diffs ``[{table, source_rows, target_rows}]``.
    Empty list = all tables match. Tables present in only one side
    are also reported (the other side's count = 0).
    """
    import duckdb as _duckdb
    import sqlalchemy as sa
    from src.db_pg import Base

    diffs: list[dict] = []
    tables = [t.name for t in Base.metadata.sorted_tables]

    duck_conn = _duckdb.connect(str(duckdb_path))
    pg_engine = _bounded_engine(target_url)
    try:
        for table in tables:
            try:
                src_count = duck_conn.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            except _duckdb.CatalogException:
                src_count = 0
            try:
                with pg_engine.connect() as pg_conn:
                    tgt_count = pg_conn.execute(
                        sa.text(f'SELECT COUNT(*) FROM "{table}"')
                    ).fetchone()[0]
            except sa.exc.ProgrammingError as exc:
                # Was: silent tgt_count=0. That collapsed to a 0==0 "match"
                # whenever source also had 0 rows, hiding typos and partial-
                # alembic states. Hard-fail with a specific message so the
                # operator can act.
                raise RuntimeError(
                    f"verify_row_counts: target table '{table}' is missing from PG "
                    f"(or the connection lacks SELECT on it). Migration cannot "
                    f"complete safely. Underlying error: {exc!s}"
                ) from exc
            if src_count != tgt_count:
                diffs.append({
                    "table": table,
                    "source_rows": src_count,
                    "target_rows": tgt_count,
                })
    finally:
        duck_conn.close()
        pg_engine.dispose()
    return diffs


def backup_duckdb(duckdb_path: Path, backups_dir: Path) -> Path:
    """gzip the DuckDB file to backups dir with timestamp.

    Returns path to backup file. Used before duckdb → side_car cutover
    so the operator has a recovery point if the side-car PG ever
    diverges and needs to be re-built from the frozen DuckDB.
    """
    import gzip
    import shutil

    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backups_dir / f"duckdb-pre-sidecar-{ts}.duckdb.gz"
    with open(duckdb_path, "rb") as src, gzip.open(out, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    return out


def backup_sidecar_pg(container_name: str, backups_dir: Path) -> Path:
    """pg_dump custom format of side-car PG, via docker exec.

    Returns path to .dump file. Used before side_car → cloud cutover.
    """
    import subprocess

    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = backups_dir / f"sidecar-pre-cloud-{ts}.dump"
    with open(out, "wb") as fp:
        result = subprocess.run(
            ["docker", "exec", container_name, "pg_dump", "-U", "agnes", "-F", "c", "agnes"],
            stdout=fp,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode()}")
    return out


def main(
    *,
    job_id: str,
    to: str,
    target_url: str,
    duckdb_path: Path,
    jobs_dir: Path,
    backups_dir: Path,
    source_url: str | None = None,
    source_backend: str | None = None,
) -> int:
    """Run the migration job. Returns process exit code.

    Dispatches by the (source_backend, target_backend) pair. Source
    defaults to ``duckdb`` for ``to=side_car`` and ``side_car`` for
    ``to=cloud`` when not specified explicitly — those are the original
    forward-only paths. The applier passes ``--source-backend`` for
    every transition so the cloud → side_car rollback (which has the
    same target=side_car as the duckdb cutover) is dispatched
    correctly.

    Source ↔ Target dispatch:
      ============  ============  ===========================
      Source        Target        Steps
      ============  ============  ===========================
      duckdb        side_car      alembic + backup duckdb + duckdb→pg + verify + flip
      duckdb        cloud         alembic + duckdb→pg + verify + flip
      side_car      cloud         alembic + backup sidecar + pg→pg + verify + flip
      cloud         side_car      alembic + pg→pg + verify + flip
      ============  ============  ===========================

    DuckDB is never a target — once an instance is on Postgres, the
    DuckDB file is treated as immutable (the backup taken before the
    cutover is the recovery artifact, not a writable target).
    """
    from src.db_state_machine import BackendState, write_backend_state

    # Derive source from the legacy "to" if not passed (back-compat for
    # the original duckdb→side_car and side_car→cloud paths).
    if source_backend is None:
        source_backend = "duckdb" if to == "side_car" else "side_car"

    writer = JobWriter(
        job_id=job_id,
        jobs_dir=jobs_dir,
        source=source_backend,
        target=to,
    )
    writer.write_initial()
    # Boundary check 0 (pre-alembic): catches cancels that arrived
    # before the subprocess even got off the ground.
    if writer.check_cancel_requested():
        writer.mark_cancelled(step="validate")
        return 0

    # Argument validation BEFORE we touch any network — every PG→PG
    # transition requires source_url. Failing here keeps the failure
    # stack short and the error message actionable rather than burying
    # it under a connect-to-bogus-target traceback.
    pg_source = source_backend in ("side_car", "cloud")
    if pg_source and not source_url:
        writer.mark_failed(
            step="validate",
            error_class="ValueError",
            error_message="--source-url is required when source is a PG backend",
        )
        return 1

    try:
        writer.update_step("alembic", progress_pct=20)
        if writer.check_cancel_requested():
            raise JobCancelled(step="alembic")
        alembic_upgrade_head(target_url)

        if source_backend == "duckdb":
            # duckdb → side_car  OR  duckdb → cloud — both copy from the
            # DuckDB file to whichever PG target the operator picked.

            if to == "side_car":
                # Backup the DuckDB file BEFORE any destructive operation on
                # the target. If anything in the rest of the pipeline crashes
                # the operator still has the source snapshot they need to
                # retry. The backup runs here — not after verify — so that a
                # crash anywhere between copy and flip leaves the operator
                # with a valid recovery point.
                writer.update_step("backup", progress_pct=15)
                if writer.check_cancel_requested():
                    raise JobCancelled(step="backup")
                backup_duckdb(duckdb_path, backups_dir)

            writer.update_step("data_copy", progress_pct=40)
            if writer.check_cancel_requested():
                raise JobCancelled(step="data_copy")
            copy_summary = copy_duckdb_to_pg(duckdb_path, target_url)

            if copy_summary.get("tables_failed"):
                writer.mark_failed(
                    step="data_copy",
                    error_class="CopyTableError",
                    error_message=(
                        "Per-table copy failed: "
                        + ", ".join(
                            f"{t['table']}={t['error']!r}"
                            for t in copy_summary["tables_failed"]
                        )
                    ),
                )
                return 1

            writer.update_step("verify", progress_pct=80)
            if writer.check_cancel_requested():
                raise JobCancelled(step="verify")
            diffs = verify_row_counts(duckdb_path, target_url)
        else:
            # side_car → cloud  OR  cloud → side_car — PG→PG.
            if source_backend == "side_car":
                writer.update_step("backup", progress_pct=30)
                if writer.check_cancel_requested():
                    raise JobCancelled(step="backup")
                try:
                    backup_sidecar_pg("agnes-postgres-1", backups_dir)
                except Exception as e:
                    # Backup failure is non-fatal — applier may have
                    # already brought postgres down, or pg_dump is
                    # missing in this image. Log via job state and
                    # proceed; the source PG keeps its data anyway.
                    _log_backup_skip(writer, str(e))

            writer.update_step("data_copy", progress_pct=40)
            if writer.check_cancel_requested():
                raise JobCancelled(step="data_copy")
            copy_summary = copy_pg_to_pg(source_url, target_url)

            if copy_summary.get("tables_failed"):
                writer.mark_failed(
                    step="data_copy",
                    error_class="CopyTableError",
                    error_message=(
                        "Per-table copy failed: "
                        + ", ".join(
                            f"{t['table']}={t['error']!r}"
                            for t in copy_summary["tables_failed"]
                        )
                    ),
                )
                return 1

            writer.update_step("verify", progress_pct=80)
            if writer.check_cancel_requested():
                raise JobCancelled(step="verify")
            diffs = verify_pg_row_counts(source_url, target_url)

        if diffs:
            writer.mark_failed(
                step="verify",
                error_class="VerifyMismatchError",
                error_message=f"Row count mismatch: {diffs[:5]}",
            )
            return 1

        # flip_backend is past point-of-no-return — no cancel check here.
        # The API endpoint already 409s on cancels for step >= flip_backend,
        # so by construction the sentinel cannot fire here legitimately.
        writer.update_step("flip_backend", progress_pct=95)
        target_state = BackendState(to)
        write_backend_state(target_state, url=target_url)

        writer.mark_success(summary=copy_summary)
        return 0

    except JobCancelled as cancel_exc:
        writer.mark_cancelled(step=cancel_exc.step)
        # Revert state machine to source — same logic as the
        # generic exception path. The API endpoint also reverts,
        # but doing it here too closes the race where the cancel
        # endpoint runs while the migrator is between step writes.
        try:
            revert_state = BackendState(source_backend) if source_backend else (
                BackendState.DUCKDB if to == "side_car" else BackendState.SIDE_CAR
            )
            write_backend_state(
                revert_state,
                url=source_url if source_backend in ("side_car", "cloud") else None,
            )
        except Exception:
            pass
        # Cancellation is a normal end — return 0. The applier looks at
        # status: cancelled in the job JSON, not the exit code.
        return 0

    except Exception as e:
        # Revert state to the source backend (best-effort). The source
        # is the authoritative "what's still working" — the in-progress
        # state was just a transient flag the API endpoint set before
        # we started. If we fail, the operator should see the original
        # backend on the next /api/admin/db/state read, not the
        # *_in_progress that hung from a half-finished migration.
        try:
            revert_state = BackendState(source_backend) if source_backend else (
                BackendState.DUCKDB if to == "side_car" else BackendState.SIDE_CAR
            )
            # For the PG sources, preserve the URL we came from so the
            # app can keep reading after restart. For DuckDB source the
            # URL is intentionally None.
            write_backend_state(
                revert_state,
                url=source_url if source_backend in ("side_car", "cloud") else None,
            )
        except Exception:
            pass
        writer.mark_failed(
            step=writer._read().get("current_step", "unknown"),
            error_class=type(e).__name__,
            error_message=str(e),
        )
        return 1


def _log_backup_skip(writer: JobWriter, reason: str) -> None:
    """Annotate the job file with a non-fatal backup skip."""
    data = writer._read()
    data.setdefault("warnings", []).append({
        "step": "backup",
        "message": f"backup skipped: {reason}",
    })
    writer._write(data)


def _resolve_source_url_from_instance_yaml() -> str | None:
    """Read ``database.url`` from /data/state/instance.yaml.

    Default source for ``--to cloud`` when the applier hasn't passed
    ``--source-url`` explicitly. Returns None if the file doesn't exist
    or lacks the key — caller decides whether to error.
    """
    import yaml as _yaml
    overlay = Path(os.environ.get("DATA_DIR", "/data")) / "state" / "instance.yaml"
    if not overlay.exists():
        return None
    try:
        data = _yaml.safe_load(overlay.read_text()) or {}
    except Exception:
        return None
    return (data.get("database") or {}).get("url")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--to", choices=["side_car", "cloud"], required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument(
        "--source-backend",
        choices=["duckdb", "side_car", "cloud"],
        help="Explicit source backend; the applier always passes this. "
             "When omitted, defaults to 'duckdb' for --to=side_car and "
             "'side_car' for --to=cloud (legacy forward-only paths).",
    )
    parser.add_argument(
        "--source-url",
        help="Source PG URL (any PG source). Defaults to instance.yaml's database.url.",
    )
    parser.add_argument("--duckdb-path", type=Path, default=Path("/data/state/system.duckdb"))
    parser.add_argument("--jobs-dir", type=Path, default=Path("/data/state/db-jobs"))
    parser.add_argument("--backups-dir", type=Path, default=Path("/data/state/backups"))
    args = parser.parse_args()

    source_backend = args.source_backend
    if source_backend is None:
        # Legacy default — keeps the old direct-invocation paths working.
        source_backend = "duckdb" if args.to == "side_car" else "side_car"

    source_url = args.source_url
    if source_backend in ("side_car", "cloud") and not source_url:
        source_url = _resolve_source_url_from_instance_yaml()

    sys.exit(main(
        job_id=args.job_id,
        to=args.to,
        target_url=args.target_url,
        duckdb_path=args.duckdb_path,
        jobs_dir=args.jobs_dir,
        backups_dir=args.backups_dir,
        source_url=source_url,
        source_backend=source_backend,
    ))
