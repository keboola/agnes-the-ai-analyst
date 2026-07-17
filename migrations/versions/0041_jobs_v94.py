"""jobs — durable job queue, worker runtime foundation (DuckDB v94).

Revision ID: 0041_jobs_v94
Revises: 0040_glossary_terms_v93

This table + ``JobsRepository``/``JobsPgRepository`` cover enqueue/get/list
+ idempotency dedup only; the claim/lease lifecycle and worker loop are
later tasks in the same wave (wave-2B: job queue + worker runtime).

``idx_jobs_idem`` is a plain (non-unique) index, not a partial unique
index. A partial unique index (``WHERE idempotency_key IS NOT NULL``)
would let a duplicate key be reused once the earlier job leaves
queued/running, but DuckDB does not support partial indexes ("Not
implemented Error: Creating partial indexes is not supported currently").
For structural parity between the two ladders, dedup is enforced in the
repository's ``enqueue()`` on both engines instead of at the DB level —
the CONTRACT is the dedup behavior, not the index.

Renumbered from 0040_jobs_v93 to 0041_jobs_v94 after upstream's
0040_glossary_terms_v93 (#920) landed first and claimed schema v93 + the
0040 slot.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0041_jobs_v94"
down_revision: str = "0040_glossary_terms_v93"
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
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_jobs_claim", "jobs", ["status", "priority", "run_after"])
    op.create_index("idx_jobs_idem", "jobs", ["idempotency_key"])


def downgrade() -> None:
    op.drop_index("idx_jobs_idem", table_name="jobs")
    op.drop_index("idx_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
