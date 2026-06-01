"""ops triad: table_registry + sync_state + sync_history

Mirrors the DuckDB shapes in src/db.py at lines 81-91 (sync_state),
106-114 (sync_history), and 290-317 (table_registry).

Revision ID: 0004_ops_triad
Revises: 0003_rbac
Create Date: 2026-05-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0004_ops_triad"
down_revision: Union[str, None] = "0003_rbac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "table_registry",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=True),
        sa.Column("bucket", sa.String(), nullable=True),
        sa.Column("source_table", sa.String(), nullable=True),
        sa.Column("source_query", sa.Text(), nullable=True),
        sa.Column("sync_strategy", sa.String(), server_default=sa.text("'full_refresh'"), nullable=False),
        sa.Column("query_mode", sa.String(), server_default=sa.text("'local'"), nullable=False),
        sa.Column("sync_schedule", sa.String(), nullable=True),
        sa.Column("profile_after_sync", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("primary_key", sa.String(), nullable=True),
        sa.Column("folder", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("registered_by", sa.String(), nullable=True),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("incremental_window_days", sa.Integer(), nullable=True),
        sa.Column("max_history_days", sa.Integer(), nullable=True),
        sa.Column("incremental_column", sa.String(), nullable=True),
        sa.Column("where_filters", sa.String(), nullable=True),
        sa.Column("partition_by", sa.String(), nullable=True),
        sa.Column("partition_granularity", sa.String(), nullable=True),
        sa.Column("initial_load_chunk_days", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_table_registry_source_type", "table_registry", ["source_type"])
    op.create_index("ix_table_registry_query_mode", "table_registry", ["query_mode"])

    op.create_table(
        "sync_state",
        sa.Column("table_id", sa.String(), nullable=False),
        sa.Column("last_sync", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows", sa.BigInteger(), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("uncompressed_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("columns", sa.Integer(), nullable=True),
        sa.Column("hash", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'ok'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("table_id"),
    )

    op.create_table(
        "sync_history",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("table_id", sa.String(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sync_history_table_id", "sync_history", ["table_id"])
    op.create_index("ix_sync_history_synced_at", "sync_history", ["synced_at"])


def downgrade() -> None:
    op.drop_index("ix_sync_history_synced_at", table_name="sync_history")
    op.drop_index("ix_sync_history_table_id", table_name="sync_history")
    op.drop_table("sync_history")

    op.drop_table("sync_state")

    op.drop_index("ix_table_registry_query_mode", table_name="table_registry")
    op.drop_index("ix_table_registry_source_type", table_name="table_registry")
    op.drop_table("table_registry")
