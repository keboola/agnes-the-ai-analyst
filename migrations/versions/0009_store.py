"""marketplace + store + flea cluster

marketplace_registry, marketplace_plugins, store_entities,
user_store_installs, user_plugin_optouts, store_submissions.

Revision ID: 0009_store
Revises: 0008_telemetry
Create Date: 2026-05-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0009_store"
down_revision: Union[str, None] = "0008_telemetry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "marketplace_registry",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("branch", sa.String(), nullable=True),
        sa.Column("token_env", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("registered_by", sa.String(), nullable=True),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_commit_sha", sa.String(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("curator_name", sa.String(), nullable=True),
        sa.Column("curator_email", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "marketplace_plugins",
        sa.Column("marketplace_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("author_name", sa.String(), nullable=True),
        sa.Column("homepage", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("source_type", sa.String(), nullable=True),
        sa.Column("source_spec", JSONB(), nullable=True),
        sa.Column("raw", JSONB(), nullable=True),
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
        sa.Column("cover_photo_url", sa.String(), nullable=True),
        sa.Column("video_url", sa.String(), nullable=True),
        sa.Column("doc_links", JSONB(), nullable=True),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.PrimaryKeyConstraint("marketplace_id", "name"),
    )

    op.create_table(
        "store_entities",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("owner_username", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("photo_path", sa.String(), nullable=True),
        sa.Column("video_url", sa.String(), nullable=True),
        sa.Column("doc_paths", JSONB(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("install_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("visibility_status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", sa.String(), nullable=True),
        sa.Column("version_no", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("version_history", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=True),
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
        sa.UniqueConstraint("owner_user_id", "name", name="uq_store_entities_owner_name"),
        sa.CheckConstraint(
            "type IN ('skill','agent','plugin')",
            name="ck_store_entities_type",
        ),
        sa.CheckConstraint(
            "visibility_status IN ('pending','approved','hidden','archived')",
            name="ck_store_entities_visibility",
        ),
    )

    op.create_table(
        "user_store_installs",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column(
            "installed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "entity_id"),
    )

    op.create_table(
        "user_plugin_optouts",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("marketplace_id", sa.String(), nullable=False),
        sa.Column("plugin_name", sa.String(), nullable=False),
        sa.Column(
            "opted_out_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "marketplace_id", "plugin_name"),
    )

    op.create_table(
        "store_submissions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=True),
        sa.Column("submitter_id", sa.String(), nullable=False),
        sa.Column("submitter_email", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("inline_checks", JSONB(), nullable=True),
        sa.Column("llm_findings", JSONB(), nullable=True),
        sa.Column("reviewed_by_model", sa.String(), nullable=True),
        sa.Column("override_by", sa.String(), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("bundle_sha256", sa.String(), nullable=True),
        sa.Column("bundle_purged_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index("idx_store_submissions_status", "store_submissions", ["status"])
    op.create_index("idx_store_submissions_entity", "store_submissions", ["entity_id"])


def downgrade() -> None:
    op.drop_index("idx_store_submissions_entity", table_name="store_submissions")
    op.drop_index("idx_store_submissions_status", table_name="store_submissions")
    op.drop_table("store_submissions")

    op.drop_table("user_plugin_optouts")
    op.drop_table("user_store_installs")
    op.drop_table("store_entities")
    op.drop_table("marketplace_plugins")
    op.drop_table("marketplace_registry")
