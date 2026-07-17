"""DuckDB-backed repository for the ``jobs`` table (durable job queue, v92).

Foundation for the wave-2B worker runtime. This module now also covers
the claim/lease/complete/fail lifecycle (worker loop itself is a later
task in the same wave).

Claim/lease/complete/fail semantics (shared contract with
``jobs_pg.py``):

- ``claim_next()`` atomically claims the oldest eligible queued job of
  the given kinds — ``status='queued'`` with ``run_after`` unset or due,
  ORDER BY ``priority DESC, created_at ASC`` — OR reclaims a ``'running'``
  job whose lease has expired (crash recovery), as long as
  ``attempts < max_attempts``. The claim sets ``status='running'``,
  ``leased_by``, ``attempts += 1``, a fresh ``lease_expires_at``, and
  ``started_at`` ONLY if it was still NULL (a reclaim preserves the
  job's original first-start timestamp).
- ``heartbeat()`` extends the lease and returns ``False`` (no-op) if the
  job is no longer ``'running'`` or is held by a different worker — the
  caller uses that signal to abandon a job that was reclaimed out from
  under it.
- ``complete()``/``fail()`` are no-ops (raise-free) when the job is not
  ``'running'`` under that exact ``worker_id``. This matters for the
  same reclaim race: a stale worker that finishes its (already
  reclaimed) job late must not clobber the new owner's state — the
  ``WHERE id = ? AND leased_by = ? AND status = 'running'`` guard on the
  mutating statement makes this atomic without needing a prior read.
- ``fail()`` with ``retry_in_seconds`` set and attempts remaining
  requeues (``status='queued'``, ``run_after=now+retry``, lease
  cleared, ``error`` recorded); otherwise (attempts exhausted, or no
  ``retry_in_seconds`` given) it finalizes to ``'failed'`` with
  ``finished_at`` set.

Idempotency-key dedup note: the schema could in principle enforce
uniqueness with a *partial* unique index
(``... WHERE idempotency_key IS NOT NULL``) so a duplicate key is only
rejected while a matching job is still queued/running (a job that has
finished/failed/been cancelled frees its key for reuse). DuckDB does not
support partial indexes ("Not implemented Error: Creating partial indexes
is not supported currently"), so dedup is enforced here instead: before
inserting, ``enqueue()`` looks for an existing row with the same
``idempotency_key`` whose status is still ``'queued'`` or ``'running'``
and returns it unchanged if found.

``jobs_pg.py`` now uses a real partial unique index + ``ON CONFLICT`` on
Postgres instead (a plain SELECT-then-INSERT there is racy under READ
COMMITTED — two concurrent transactions can both miss each other's
uncommitted row). DuckDB's single-writer model doesn't have that
cross-transaction race, but the check-then-insert here is still not
atomic across *threads* sharing one connection, so ``_JOBS_LOCK``
serializes the whole critical section.

``_JOBS_LOCK`` is a MODULE-level lock (mirroring the ``_rebuild_lock``
pattern in ``src/orchestrator.py``, which is also module-level), not a
``self._lock`` on the repository instance. The factory
(``src.repositories.jobs_repo()``) builds a fresh ``JobsRepository``
per call, all wrapping the *same* underlying connection
(``get_system_db()``) — an instance-level lock would give each caller
its own, unshared ``threading.Lock()`` and serialize nothing (empirically
confirmed: 8 threads, each with its own repo instance, produced 8 rows
for one idempotency key). A module-level lock is shared by every
instance regardless of how many separate ``JobsRepository`` objects
wrap the connection, so it actually protects the critical section. The
CONTRACT shared with the PG side is the dedup *behavior* (matching key +
queued/running status returns the existing row, no insert), not the
mechanism.

The SAME lock (not a second, independent one) guards ``claim_next()``,
``heartbeat()``, ``complete()``, and ``fail()`` too: DuckDB connections
are not safe for concurrent ``execute()`` calls from multiple threads,
so every multi-statement (or otherwise non-trivially-atomic) critical
section touching this shared connection must serialize against every
other one — two separate locks would each protect their own section
while leaving them free to interleave with each other on the connection
object itself.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import duckdb

#: Serializes every mutating critical section (enqueue, claim_next,
#: heartbeat, complete, fail) across ALL JobsRepository instances (see
#: module docstring for why this must be module-level, not per-instance).
_JOBS_LOCK = threading.Lock()


class JobsRepository:
    #: Worker-runtime lane identifiers (Task 3 registers job kinds against
    #: one of these). Plain string constants — duplicated (not imported)
    #: on ``JobsPgRepository`` per the method/attribute-mirroring rule so
    #: neither backend module depends on the other.
    HEAVY_LANE = "heavy"
    LIGHT_LANE = "light"

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    @staticmethod
    def _decode(d: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(d.get("payload_json"), str):
            try:
                d["payload_json"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
            except (TypeError, ValueError):
                d["payload_json"] = {}
        return d

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return self._decode(dict(zip(columns, row)))

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [self._decode(dict(zip(columns, r))) for r in rows]

    def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        priority: int = 0,
        run_after: Optional[datetime] = None,
        max_attempts: int = 3,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a queued job and return its row.

        If ``idempotency_key`` matches an existing job whose status is
        still ``'queued'`` or ``'running'``, that row is returned
        unchanged — no new insert (dedup). See the module docstring for
        why this check lives here rather than in a DB constraint, and
        why it's guarded by the module-level ``_JOBS_LOCK``.
        """
        with _JOBS_LOCK:
            if idempotency_key is not None:
                existing = self.conn.execute(
                    """SELECT * FROM jobs
                       WHERE idempotency_key = ? AND status IN ('queued', 'running')
                       ORDER BY created_at LIMIT 1""",
                    [idempotency_key],
                ).fetchone()
                existing_row = self._row_to_dict(existing)
                if existing_row is not None:
                    return existing_row

            job_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc)
            self.conn.execute(
                """INSERT INTO jobs
                   (id, kind, payload_json, status, priority, run_after,
                    attempts, max_attempts, idempotency_key, created_at)
                   VALUES (?, ?, ?, 'queued', ?, ?, 0, ?, ?, ?)""",
                [
                    job_id,
                    kind,
                    json.dumps(payload or {}),
                    priority,
                    run_after,
                    max_attempts,
                    idempotency_key,
                    now,
                ],
            )
            row = self.get(job_id)
            assert row is not None  # just inserted under our own transaction
            return row

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", [job_id]).fetchone()
        return self._row_to_dict(row)

    def list(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: List[Any] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    def claim_next(
        self,
        *,
        kinds: List[str],
        worker_id: str,
        lease_seconds: int = 120,
    ) -> Optional[Dict[str, Any]]:
        """Atomically claim the oldest eligible queued job of ``kinds``.

        Eligible = ``status='queued'`` with ``run_after`` unset or due, OR
        a ``'running'`` job whose lease has expired and which hasn't yet
        exhausted its attempts (crash-recovery reclaim). Ordered by
        ``priority DESC, created_at ASC``. See the module docstring for
        why the read-then-write here is guarded by the module-level
        ``_JOBS_LOCK`` rather than relying on DuckDB's single-writer
        model alone (that guarantee is about cross-process/transaction
        safety, not concurrent threads sharing one connection object).
        """
        if not kinds:
            return None
        with _JOBS_LOCK:
            now = datetime.now(timezone.utc)
            placeholders = ",".join(["?"] * len(kinds))
            row = self.conn.execute(
                f"""SELECT * FROM jobs
                    WHERE kind IN ({placeholders})
                      AND (
                        (status = 'queued' AND (run_after IS NULL OR run_after <= ?))
                        OR (status = 'running' AND lease_expires_at < ? AND attempts < max_attempts)
                      )
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1""",
                [*kinds, now, now],
            ).fetchone()
            job = self._row_to_dict(row)
            if job is None:
                return None
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            self.conn.execute(
                """UPDATE jobs
                   SET status = 'running',
                       leased_by = ?,
                       started_at = COALESCE(started_at, ?),
                       attempts = attempts + 1,
                       lease_expires_at = ?
                   WHERE id = ?""",
                [worker_id, now, lease_expires_at, job["id"]],
            )
            return self.get(job["id"])

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int = 120) -> bool:
        """Extend the lease on a running job. Returns ``False`` (no-op) if
        the job is no longer ``'running'`` or is held by a different
        worker — the caller uses that to abandon a job reclaimed out
        from under it."""
        with _JOBS_LOCK:
            now = datetime.now(timezone.utc)
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            claimed = self.conn.execute(
                """UPDATE jobs SET lease_expires_at = ?
                   WHERE id = ? AND leased_by = ? AND status = 'running'
                   RETURNING id""",
                [lease_expires_at, job_id, worker_id],
            ).fetchall()
            return bool(claimed)

    def complete(self, job_id: str, worker_id: str) -> None:
        """Mark a running job done. No-op (raise-free) if the job is not
        currently ``'running'`` under this exact ``worker_id`` — a stale
        worker finishing a job that was already reclaimed must not
        clobber the new owner's state."""
        with _JOBS_LOCK:
            now = datetime.now(timezone.utc)
            self.conn.execute(
                """UPDATE jobs
                   SET status = 'done', finished_at = ?, lease_expires_at = NULL
                   WHERE id = ? AND leased_by = ? AND status = 'running'""",
                [now, job_id, worker_id],
            )

    def fail(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        retry_in_seconds: Optional[int] = None,
    ) -> None:
        """Record a failure. No-op (raise-free) if the job is not
        currently ``'running'`` under this exact ``worker_id`` (see
        ``complete()``).

        If attempts remain (``attempts < max_attempts``) AND
        ``retry_in_seconds`` is given, requeues the job (``'queued'``,
        ``run_after=now+retry_in_seconds``, lease cleared, ``error``
        recorded). Otherwise finalizes to ``'failed'`` with
        ``finished_at`` set.
        """
        with _JOBS_LOCK:
            now = datetime.now(timezone.utc)
            job = self.conn.execute(
                """SELECT attempts, max_attempts FROM jobs
                   WHERE id = ? AND leased_by = ? AND status = 'running'""",
                [job_id, worker_id],
            ).fetchone()
            if job is None:
                return  # stale worker (already reclaimed) — no-op
            attempts, max_attempts = job
            if attempts < max_attempts and retry_in_seconds is not None:
                run_after = now + timedelta(seconds=retry_in_seconds)
                self.conn.execute(
                    """UPDATE jobs
                       SET status = 'queued', run_after = ?, lease_expires_at = NULL,
                           leased_by = NULL, error = ?
                       WHERE id = ? AND leased_by = ? AND status = 'running'""",
                    [run_after, error, job_id, worker_id],
                )
            else:
                self.conn.execute(
                    """UPDATE jobs
                       SET status = 'failed', finished_at = ?, lease_expires_at = NULL, error = ?
                       WHERE id = ? AND leased_by = ? AND status = 'running'""",
                    [now, error, job_id, worker_id],
                )
