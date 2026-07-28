"""data_packages + memory_* + recipes + user_stack_subscriptions.

Adds the remaining tables required by the OSS DuckDB->PG migration:
  - data_packages + data_package_tables (bridge to table_registry)
  - memory_domains + knowledge_item_domains (bridge to knowledge_items)
    + memory_domain_suggestions (admin queue)
  - recipes
  - user_stack_subscriptions

Revision ID: 0011_data_packages
Revises: 0010_knowledge
Create Date: 2026-05-27

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0011_data_packages"
down_revision: Union[str, None] = "0010_knowledge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- data_packages (parent) ---
    op.create_table(
        "data_packages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("color", sa.String(), nullable=True),
        sa.Column("cover_image_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'prod'"), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_name", sa.String(), nullable=True),
        sa.Column("owner_team", sa.String(), nullable=True),
        sa.Column("tags", JSONB(), nullable=True),
        sa.Column("long_description", sa.Text(), nullable=True),
        sa.Column("when_to_use", JSONB(), nullable=True),
        sa.Column("when_not_to_use", JSONB(), nullable=True),
        sa.Column("example_questions", JSONB(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
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
        sa.UniqueConstraint("slug"),
    )

    # --- memory_domains (parent) ---
    op.create_table(
        "memory_domains",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("color", sa.String(), nullable=True),
        sa.Column("cover_image_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'prod'"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
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
        sa.UniqueConstraint("slug"),
    )

    # --- memory_domain_suggestions (independent admin queue) ---
    op.create_table(
        "memory_domain_suggestions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("created_domain_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_memory_domain_suggestions_status",
        "memory_domain_suggestions",
        ["status"],
    )

    # --- recipes (parent) ---
    op.create_table(
        "recipes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("color", sa.String(), nullable=True),
        sa.Column("sql_template", sa.Text(), nullable=True),
        sa.Column("related_table_ids", JSONB(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'prod'"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
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
        sa.UniqueConstraint("slug"),
    )

    # --- user_stack_subscriptions (independent) ---
    op.create_table(
        "user_stack_subscriptions",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column(
            "subscribed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "resource_type", "resource_id"),
    )
    op.create_index(
        "idx_user_stack_subscriptions_user",
        "user_stack_subscriptions",
        ["user_id"],
    )

    # --- bridges (created after parents) ---
    op.create_table(
        "data_package_tables",
        sa.Column("package_id", sa.String(), nullable=False),
        sa.Column("table_id", sa.String(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("added_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["package_id"], ["data_packages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("package_id", "table_id"),
    )
    op.create_index(
        "idx_data_package_tables_table",
        "data_package_tables",
        ["table_id"],
    )

    op.create_table(
        "knowledge_item_domains",
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("domain_id", sa.String(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("added_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["domain_id"], ["memory_domains.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id", "domain_id"),
    )
    op.create_index(
        "idx_knowledge_item_domains_domain",
        "knowledge_item_domains",
        ["domain_id"],
    )


def downgrade() -> None:
    # Bridges first (FK cascade safety)
    op.drop_index("idx_knowledge_item_domains_domain", table_name="knowledge_item_domains")
    op.drop_table("knowledge_item_domains")
    op.drop_index("idx_data_package_tables_table", table_name="data_package_tables")
    op.drop_table("data_package_tables")

    # Then independent tables
    op.drop_index("idx_user_stack_subscriptions_user", table_name="user_stack_subscriptions")
    op.drop_table("user_stack_subscriptions")
    op.drop_table("recipes")
    op.drop_index("idx_memory_domain_suggestions_status", table_name="memory_domain_suggestions")
    op.drop_table("memory_domain_suggestions")

    # Finally parent tables referenced by the bridges
    op.drop_table("memory_domains")
    op.drop_table("data_packages")
