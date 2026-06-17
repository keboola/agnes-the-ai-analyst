"""Collections foundation (DuckDB v82 parity).

Creates three new tables that form the Collections (bring-your-files) feature:

- ``file_corpora``: a Collection container (slug, name, description,
  created_by, soft-delete via deleted_at).
- ``corpus_files``: per-uploaded-file row with a four-state processing
  lifecycle (pending | processing | indexed | rejected).
- ``corpus_chunks``: prose chunks + 384-dim embedding column for future
  vector retrieval (repo deferred to Retrieval slice; table created now
  so the single-migration-per-build-run constraint is met).

Mirrors DuckDB ``_v81_to_v82``. All additive; downgrade drops in reverse.

Revision ID: 0029_collections_v82
Revises: 0028_memory_mining_consent_v81
Create Date: 2026-06-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_collections_v82"
down_revision: Union[str, None] = "0028_memory_mining_consent_v81"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "file_corpora",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    op.create_table(
        "corpus_files",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("file_type", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("storage_path", sa.String(), nullable=True),
        sa.Column(
            "processing_status",
            sa.String(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("processing_detail", sa.String(), nullable=True),
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
    )

    op.create_table(
        "corpus_chunks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=True),
        sa.Column("text", sa.String(), nullable=True),
        # PG side: real[] (float4) to match the DuckDB ``FLOAT[384]`` storage
        # precision so embeddings round-trip identically on both backends and
        # cosine scores don't diverge. pgvector vector(384) is a Retrieval-slice
        # option.
        sa.Column("embedding", sa.ARRAY(sa.REAL()), nullable=True),
        sa.Column("section_path", sa.String(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("bbox", sa.String(), nullable=True),
        sa.Column("metadata", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("corpus_chunks")
    op.drop_table("corpus_files")
    op.drop_table("file_corpora")
