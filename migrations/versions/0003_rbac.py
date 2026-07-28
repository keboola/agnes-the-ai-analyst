"""users + RBAC tables

Adds ``users``, ``user_groups``, ``user_group_members``, ``resource_grants``.
Mirrors the DuckDB shapes in ``src/db.py``. FK directions match the v14
DuckDB layout: group memberships and grants both reference user_groups(id).

Revision ID: 0003_rbac
Revises: 0002_audit_log
Create Date: 2026-05-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0003_rbac"
down_revision: Union[str, None] = "0002_audit_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("setup_token", sa.String(), nullable=True),
        sa.Column("setup_token_created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reset_token", sa.String(), nullable=True),
        sa.Column("reset_token_created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivated_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("onboarded", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("last_pull_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "user_groups",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "user_group_members",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("added_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["user_groups.id"]),
        sa.PrimaryKeyConstraint("user_id", "group_id"),
    )
    op.create_index("ix_user_group_members_user_id", "user_group_members", ["user_id"])
    op.create_index("ix_user_group_members_group_id", "user_group_members", ["group_id"])
    op.create_index("ix_user_group_members_source", "user_group_members", ["source"])

    op.create_table(
        "resource_grants",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("assigned_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["user_groups.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id",
            "resource_type",
            "resource_id",
            name="uq_resource_grants_group_type_id",
        ),
    )
    op.create_index("ix_resource_grants_group_id", "resource_grants", ["group_id"])
    op.create_index("ix_resource_grants_resource_type", "resource_grants", ["resource_type"])
    op.create_index("ix_resource_grants_resource_id", "resource_grants", ["resource_id"])


def downgrade() -> None:
    op.drop_index("ix_resource_grants_resource_id", table_name="resource_grants")
    op.drop_index("ix_resource_grants_resource_type", table_name="resource_grants")
    op.drop_index("ix_resource_grants_group_id", table_name="resource_grants")
    op.drop_table("resource_grants")

    op.drop_index("ix_user_group_members_source", table_name="user_group_members")
    op.drop_index("ix_user_group_members_group_id", table_name="user_group_members")
    op.drop_index("ix_user_group_members_user_id", table_name="user_group_members")
    op.drop_table("user_group_members")

    op.drop_table("user_groups")
    op.drop_table("users")
