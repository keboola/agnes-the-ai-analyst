"""knowledge_digests table — maintained digests (DuckDB v89 parity, K4, #799)

Admin-defined digest documents the scheduler regenerates via LLM when their
source corpora change. status: pending (never generated) | fresh | stale
(sources changed but regeneration failed/deferred — output_md is the last
good generation, status_reason says why). source_corpus_ids is a JSON array
stored as text.

Mirrors DuckDB ``_v88_to_v89``. Additive only; downgrade drops the table.

Revision ID: 0036_knowledge_digests_v89
Revises: 0035_parent_file_id_v88
Create Date: 2026-07-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036_knowledge_digests_v89"
down_revision: Union[str, None] = "0035_parent_file_id_v88"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_digests",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("source_corpus_ids", sa.String(), nullable=True),
        sa.Column("output_md", sa.Text(), nullable=True),
        sa.Column("source_fingerprint", sa.String(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column(
            "status",
            sa.String(),
            server_default=sa.text("'pending'"),
            nullable=True,
        ),
        sa.Column("status_reason", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_digests")
