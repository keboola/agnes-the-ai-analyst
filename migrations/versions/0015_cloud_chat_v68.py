"""Cloud-chat tables (DuckDB v68 parity).

Creates the three tables introduced on the DuckDB side in schema version 68:

  v68 — chat_sessions: per-session chat transcript headers.
         chat_messages: individual messages, FK → chat_sessions ON DELETE CASCADE.
         user_workdirs: per-user workspace init markers.

The Postgres schema carries constraints the DuckDB 1.5.x side cannot express:
  - chat_messages.session_id FK with ON DELETE CASCADE (DuckDB lacks CASCADE;
    ChatRepository deletes child messages by hand).
  - Partial unique indexes for per-surface Slack uniqueness (DuckDB lacks
    filtered unique indexes; ChatRepository enforces it in app code):
      * unique slack_channel_id WHERE surface='slack_dm'
      * unique (slack_channel_id, slack_thread_ts) WHERE surface='slack_thread'

Revision ID: 0015_cloud_chat_v68
Revises: 0014_cowork_mcp_v63_v67
Create Date: 2026-06-02
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015_cloud_chat_v68"
down_revision: Union[str, None] = "0014_cowork_mcp_v63_v67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # v68: chat_sessions
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("slack_channel_id", sa.String(), nullable=True),
        sa.Column("slack_thread_ts", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "message_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "archived",
            sa.Boolean(),
            server_default=sa.text("FALSE"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_chat_sessions_user", "chat_sessions", ["user_email", "last_message_at"]
    )
    op.create_index(
        "uq_chat_sessions_slack_dm",
        "chat_sessions",
        ["slack_channel_id"],
        unique=True,
        postgresql_where=sa.text("surface = 'slack_dm'"),
    )
    op.create_index(
        "uq_chat_sessions_slack_thread",
        "chat_sessions",
        ["slack_channel_id", "slack_thread_ts"],
        unique=True,
        postgresql_where=sa.text("surface = 'slack_thread'"),
    )

    # v68: chat_messages
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            sa.String(),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls", JSONB, nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_chat_messages_session", "chat_messages", ["session_id", "created_at"]
    )

    # v68: user_workdirs
    op.create_table(
        "user_workdirs",
        sa.Column("user_email", sa.String(), primary_key=True, nullable=False),
        sa.Column("last_init_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("marketplace_sha", sa.String(), nullable=True),
        sa.Column("initial_workspace_sha", sa.String(), nullable=True),
        sa.Column("agnes_version_at_init", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("user_workdirs")
    op.drop_index("idx_chat_messages_session", "chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("uq_chat_sessions_slack_thread", "chat_sessions")
    op.drop_index("uq_chat_sessions_slack_dm", "chat_sessions")
    op.drop_index("idx_chat_sessions_user", "chat_sessions")
    op.drop_table("chat_sessions")
