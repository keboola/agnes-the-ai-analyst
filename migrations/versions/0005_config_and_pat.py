"""config cluster: metric_definitions + instance_templates + personal_access_tokens

Mirrors src/db.py:329-352 (metric_definitions), 365-377 (PATs), 496-502
(instance_templates).

Revision ID: 0005_config_and_pat
Revises: 0004_ops_triad
Create Date: 2026-05-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision: str = "0005_config_and_pat"
down_revision: Union[str, None] = "0004_ops_triad"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "metric_definitions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("type", sa.String(), server_default=sa.text("'sum'"), nullable=False),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("grain", sa.String(), server_default=sa.text("'monthly'"), nullable=False),
        sa.Column("table_name", sa.String(), nullable=True),
        sa.Column("tables", ARRAY(sa.String()), nullable=True),
        sa.Column("expression", sa.String(), nullable=True),
        sa.Column("time_column", sa.String(), nullable=True),
        sa.Column("dimensions", ARRAY(sa.String()), nullable=True),
        sa.Column("filters", ARRAY(sa.String()), nullable=True),
        sa.Column("synonyms", ARRAY(sa.String()), nullable=True),
        sa.Column("notes", ARRAY(sa.String()), nullable=True),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("sql_variants", JSONB(), nullable=True),
        sa.Column("validation", JSONB(), nullable=True),
        sa.Column("source", sa.String(), server_default=sa.text("'manual'"), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_metric_definitions_category", "metric_definitions", ["category"])
    op.create_index("ix_metric_definitions_name", "metric_definitions", ["name"])

    op.create_table(
        "instance_templates",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("previous_content", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("scopes", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_personal_access_tokens_user_id", "personal_access_tokens", ["user_id"])
    op.create_index("ix_personal_access_tokens_prefix", "personal_access_tokens", ["prefix"])


def downgrade() -> None:
    op.drop_index("ix_personal_access_tokens_prefix", table_name="personal_access_tokens")
    op.drop_index("ix_personal_access_tokens_user_id", table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")

    op.drop_table("instance_templates")

    op.drop_index("ix_metric_definitions_name", table_name="metric_definitions")
    op.drop_index("ix_metric_definitions_category", table_name="metric_definitions")
    op.drop_table("metric_definitions")
