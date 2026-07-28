"""telemetry + observability cluster

session_processor_state, user_observability_views, usage_events,
usage_session_summary, usage_tool_daily, usage_marketplace_item_daily,
usage_marketplace_item_window.

Revision ID: 0008_telemetry
Revises: 0007_misc
Create Date: 2026-05-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0008_telemetry"
down_revision: Union[str, None] = "0007_misc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_processor_state",
        sa.Column("processor_name", sa.String(), nullable=False),
        sa.Column("session_file", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("items_extracted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("file_hash", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("processor_name", "session_file"),
    )

    op.create_table(
        "user_observability_views",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("query_json", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_obs_views_user_name"),
    )
    op.create_index("idx_obs_views_user", "user_observability_views", ["user_id", "created_at"])

    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("session_file", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("event_uuid", sa.String(), nullable=True),
        sa.Column("parent_uuid", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("skill_name", sa.String(), nullable=True),
        sa.Column("subagent_type", sa.String(), nullable=True),
        sa.Column("command_name", sa.String(), nullable=True),
        sa.Column("is_error", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("ref_id", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("cwd", sa.String(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processor_version", sa.Integer(), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("friction_tags", JSONB(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_usage_events_session", "usage_events", ["session_id"])
    op.create_index("idx_usage_events_user_time", "usage_events", ["username", "occurred_at"])
    op.create_index("idx_usage_events_tool", "usage_events", ["tool_name"])
    op.create_index("idx_usage_events_skill", "usage_events", ["skill_name"])
    op.create_index("idx_usage_events_ref", "usage_events", ["source", "ref_id"])
    op.create_index("idx_usage_events_user_id", "usage_events", ["user_id"])

    op.create_table(
        "usage_session_summary",
        sa.Column("session_file", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_seconds", sa.Integer(), nullable=True),
        sa.Column("wall_seconds", sa.Integer(), nullable=True),
        sa.Column("user_messages", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("assistant_messages", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tool_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tool_errors", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("skill_invocations", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("subagent_dispatches", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("mcp_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("slash_commands", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("distinct_tools", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("distinct_skills", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("primary_model", sa.String(), nullable=True),
        sa.Column("processor_version", sa.Integer(), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_creation_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("session_file"),
    )
    op.create_index("idx_usage_session_user", "usage_session_summary", ["username"])
    op.create_index("idx_usage_session_started", "usage_session_summary", ["started_at"])
    op.create_index("idx_usage_session_user_id", "usage_session_summary", ["user_id"])

    op.create_table(
        "usage_tool_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("invocations", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("distinct_users", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("distinct_sessions", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("day", "tool_name", "source"),
    )

    op.create_table(
        "usage_marketplace_item_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("parent_plugin", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("distinct_users", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("day", "source", "type", "parent_plugin", "name"),
    )
    op.create_index(
        "idx_mid_lookup",
        "usage_marketplace_item_daily",
        ["source", "type", "parent_plugin", "name"],
    )

    op.create_table(
        "usage_marketplace_item_window",
        sa.Column("period_label", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("parent_plugin", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("invocations", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("distinct_users", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("period_label", "source", "type", "parent_plugin", "name"),
    )
    op.create_index(
        "idx_miw_lookup",
        "usage_marketplace_item_window",
        ["period_label", "source", "type"],
    )


def downgrade() -> None:
    op.drop_index("idx_miw_lookup", table_name="usage_marketplace_item_window")
    op.drop_table("usage_marketplace_item_window")

    op.drop_index("idx_mid_lookup", table_name="usage_marketplace_item_daily")
    op.drop_table("usage_marketplace_item_daily")

    op.drop_table("usage_tool_daily")

    op.drop_index("idx_usage_session_user_id", table_name="usage_session_summary")
    op.drop_index("idx_usage_session_started", table_name="usage_session_summary")
    op.drop_index("idx_usage_session_user", table_name="usage_session_summary")
    op.drop_table("usage_session_summary")

    op.drop_index("idx_usage_events_user_id", table_name="usage_events")
    op.drop_index("idx_usage_events_ref", table_name="usage_events")
    op.drop_index("idx_usage_events_skill", table_name="usage_events")
    op.drop_index("idx_usage_events_tool", table_name="usage_events")
    op.drop_index("idx_usage_events_user_time", table_name="usage_events")
    op.drop_index("idx_usage_events_session", table_name="usage_events")
    op.drop_table("usage_events")

    op.drop_index("idx_obs_views_user", table_name="user_observability_views")
    op.drop_table("user_observability_views")

    op.drop_table("session_processor_state")
