"""lookup cluster: view_ownership + column_metadata + bq_metadata_cache + user_sync_settings

Mirrors src/db.py:100-104, 354-363, 689-710, 116-124.

Revision ID: 0006_lookup
Revises: 0005_config_and_pat
Create Date: 2026-05-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0006_lookup"
down_revision: Union[str, None] = "0005_config_and_pat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "view_ownership",
        sa.Column("view_name", sa.String(), nullable=False),
        sa.Column("source_name", sa.String(), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("view_name"),
    )

    op.create_table(
        "column_metadata",
        sa.Column("table_id", sa.String(), nullable=False),
        sa.Column("column_name", sa.String(), nullable=False),
        sa.Column("basetype", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("confidence", sa.String(), server_default=sa.text("'manual'"), nullable=False),
        sa.Column("source", sa.String(), server_default=sa.text("'manual'"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("table_id", "column_name"),
    )
    op.create_index("ix_column_metadata_table_id", "column_metadata", ["table_id"])

    op.create_table(
        "bq_metadata_cache",
        sa.Column("table_id", sa.String(), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("partition_by", sa.String(), nullable=True),
        sa.Column("clustered_by", JSONB(), nullable=True),
        sa.Column("entity_type", sa.String(), nullable=True),
        sa.Column("known_columns", JSONB(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_msg", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("table_id"),
    )

    op.create_table(
        "user_sync_settings",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("dataset", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("table_mode", sa.String(), server_default=sa.text("'all'"), nullable=False),
        sa.Column("tables", JSONB(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("user_id", "dataset"),
    )
    op.create_index("ix_user_sync_settings_user_id", "user_sync_settings", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_sync_settings_user_id", table_name="user_sync_settings")
    op.drop_table("user_sync_settings")

    op.drop_table("bq_metadata_cache")

    op.drop_index("ix_column_metadata_table_id", table_name="column_metadata")
    op.drop_table("column_metadata")

    op.drop_table("view_ownership")
