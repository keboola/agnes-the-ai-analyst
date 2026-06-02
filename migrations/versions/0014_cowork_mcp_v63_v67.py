"""Agnes Cowork + Universal MCP tables (DuckDB v63–v67 parity).

Creates the following tables introduced on the DuckDB side in schema
versions 63 through 67:

  v63 — setup_tokens: short-lived one-time tokens for the Cowork one-click
         setup flow (POST /api/user/cowork-bundle → POST /api/auth/exchange-setup-token).

  v64 — mcp_sources: external MCP servers registered for inbound tool ingestion.
         tool_registry: curated tools (materialize or passthrough mode).
         tool_grants: per-group ACL for passthrough tools.

  v65 — mcp_secrets: server-wide vault for MCP source auth credentials.

  v66 — mcp_user_secrets: per-user credential store for per_user-scope sources.
         mcp_sources.scope: 'shared' (default) | 'per_user' routing column.

  v67 — data_package_tools: M:N junction between data_packages and tool_registry.

Revision ID: 0014_cowork_mcp_v63_v67
Revises: 0013_resource_grants_per_type_fk
Create Date: 2026-06-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014_cowork_mcp_v63_v67"
down_revision: Union[str, None] = "0013_resource_grants_per_type_fk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # v63: setup_tokens
    op.create_table(
        "setup_tokens",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index("ix_setup_tokens_user_id", "setup_tokens", ["user_id"])

    # v64: mcp_sources
    op.create_table(
        "mcp_sources",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), unique=True, nullable=False),
        sa.Column("transport", sa.String(), nullable=False),
        sa.Column("command", sa.String(), nullable=True),
        sa.Column("args", JSONB, nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("auth_method", sa.String(), nullable=True),
        sa.Column("auth_secret_env", sa.String(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("TRUE"),
            nullable=False,
        ),
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

    # v64: tool_registry
    op.create_table(
        "tool_registry",
        sa.Column("tool_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("original_name", sa.String(), nullable=False),
        sa.Column("exposed_name", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("table_id", sa.String(), nullable=True),
        sa.Column("input_schema", JSONB, nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "mutating",
            sa.Boolean(),
            server_default=sa.text("FALSE"),
            nullable=False,
        ),
        sa.Column("pii_fields", JSONB, nullable=True),
        sa.Column("rate_limit_pm", sa.Integer(), nullable=True),
        sa.Column("schedule", sa.String(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("TRUE"),
            nullable=False,
        ),
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
    op.create_index("ix_tool_registry_source_id", "tool_registry", ["source_id"])

    # v64: tool_grants
    op.create_table(
        "tool_grants",
        sa.Column("tool_id", sa.String(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("tool_id", "group_id"),
    )
    op.create_index("ix_tool_grants_group_id", "tool_grants", ["group_id"])

    # v65: mcp_secrets
    op.create_table(
        "mcp_secrets",
        sa.Column("source_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("secret_value_enc", sa.LargeBinary(), nullable=False),
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

    # v66: mcp_user_secrets
    op.create_table(
        "mcp_user_secrets",
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("secret_value_enc", sa.LargeBinary(), nullable=False),
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
        sa.PrimaryKeyConstraint("source_id", "user_id"),
    )

    # v66: scope column on mcp_sources
    op.add_column(
        "mcp_sources",
        sa.Column(
            "scope",
            sa.String(),
            server_default=sa.text("'shared'"),
            nullable=True,
        ),
    )

    # v67: data_package_tools
    op.create_table(
        "data_package_tools",
        sa.Column(
            "package_id",
            sa.String(),
            sa.ForeignKey("data_packages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_id", sa.String(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("package_id", "tool_id"),
    )
    op.create_index("idx_data_package_tools_tool", "data_package_tools", ["tool_id"])


def downgrade() -> None:
    op.drop_index("idx_data_package_tools_tool", "data_package_tools")
    op.drop_table("data_package_tools")
    op.drop_column("mcp_sources", "scope")
    op.drop_table("mcp_user_secrets")
    op.drop_table("mcp_secrets")
    op.drop_index("ix_tool_grants_group_id", "tool_grants")
    op.drop_table("tool_grants")
    op.drop_index("ix_tool_registry_source_id", "tool_registry")
    op.drop_table("tool_registry")
    op.drop_table("mcp_sources")
    op.drop_index("ix_setup_tokens_user_id", "setup_tokens")
    op.drop_table("setup_tokens")
