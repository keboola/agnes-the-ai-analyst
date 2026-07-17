"""jobs — durable job queue, worker runtime foundation (DuckDB v93).

Revision ID: 0040_jobs_v93
Revises: 0039_mcp_connect_hint_v92

This table + ``JobsRepository``/``JobsPgRepository`` cover enqueue/get/list
+ idempotency dedup only; the claim/lease lifecycle and worker loop are
later tasks in the same wave (wave-2B: job queue + worker runtime).

``idx_jobs_idem`` is a *partial unique* index on Postgres:
``WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running')``.
A duplicate key can be reused once the earlier job leaves queued/running
(the index simply stops covering that row), but two concurrent enqueues
of the same key while a queued/running row exists cannot both insert —
Postgres's unique-index conflict check makes that atomic, which a plain
SELECT-then-INSERT in application code cannot (READ COMMITTED lets two
transactions both miss each other's uncommitted row; 8 concurrent
enqueues of the same key produced 8 rows before this fix). See
``JobsPgRepository.enqueue()`` for the matching ``INSERT ... ON CONFLICT
... DO NOTHING`` that uses this index as its arbiter.

The DuckDB ladder (``src/db.py``) deliberately keeps ``idx_jobs_idem`` as
a **plain** (non-unique, non-partial) index — DuckDB does not support
partial indexes ("Not implemented Error: Creating partial indexes is not
supported currently") — and continues to enforce dedup in
``JobsRepository.enqueue()`` with a lock guarding the check-then-insert,
safe under DuckDB's single-writer model. The two ladders are
intentionally asymmetric here: the CONTRACT is the dedup *behavior*
(matching key + queued/running status returns the existing row, no
insert), not the index shape.

``lease_token`` is a fresh uuid4 minted by ``claim_next()`` on every
claim (including a same-worker reclaim). ``heartbeat()``/``complete()``/
``fail()`` guard on ``lease_token = ? AND status = 'running'`` rather
than ``leased_by = ?`` — all lane slots in one worker process share the
same ``leased_by`` (worker_id = hostname:pid), so a worker_id-only guard
cannot distinguish a stale slot's late call from a same-process reclaim
of the same job by a DIFFERENT slot (empirically reproduced
double-execution bug). ``leased_by`` is kept for audit/logging only.

Renumbered from the original 0039_jobs_v92 to 0040_jobs_v93 after
upstream's 0039_mcp_connect_hint_v92 (#919) landed first and claimed
schema v92 + the 0039 slot.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0040_jobs_v93"
down_revision: str = "0039_mcp_connect_hint_v92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload_json", sa.String(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_by", sa.String(), nullable=True),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_jobs_claim", "jobs", ["status", "priority", "run_after"])
    op.create_index(
        "idx_jobs_idem",
        "jobs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL AND status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("idx_jobs_idem", table_name="jobs")
    op.drop_index("idx_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
