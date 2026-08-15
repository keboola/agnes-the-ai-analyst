"""semantic_models, semantic_sources, data_package_semantic_models

Mirrors DuckDB ``_v116_to_v117``. Pure additive DDL, no backfill.

Revision ID: 0064_sem_models_v117
Revises: 0063_access_policy_columns_v116
Create Date: 2026-08-13

Note on the down_revision: this chains after
``0062_knowledge_domains_backfill`` rather than
``0061_agent_status_backfill_v115`` (SCHEMA_VERSION 115, the head at the time
the plan for this migration was written) because that data-only backfill
landed on ``main`` first and does not bump ``SCHEMA_VERSION`` — the DuckDB
ladder position (v115→v116) is unaffected, only the Alembic revision number
moved from 0062 to 0063.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064_sem_models_v117"
down_revision: Union[str, None] = "0063_access_policy_columns_v116"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "semantic_models",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("document_json", postgresql.JSONB()),
        sa.Column("spec_version", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String()),
        sa.Column("status", sa.String(), nullable=False, server_default="valid"),
        sa.Column("validation_errors", postgresql.JSONB()),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_semantic_models_origin",
        "semantic_models",
        ["source", "source_ref", "slug"],
        unique=True,
    )
    op.create_table(
        "semantic_sources",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("adapter", sa.String(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true()),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_sync_status", sa.String()),
        sa.Column("last_sync_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_table(
        "data_package_semantic_models",
        sa.Column("package_id", sa.String(), primary_key=True),
        sa.Column("model_id", sa.String(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("data_package_semantic_models")
    op.drop_table("semantic_sources")
    op.drop_index("idx_semantic_models_origin", table_name="semantic_models")
    op.drop_table("semantic_models")
