"""Live co-drive foundation (DuckDB v69 parity).

Additive-only, reaching the same endpoint as DuckDB ``_v68_to_v69``:
  - chat_sessions.is_co_session / ephemeral (BOOLEAN NOT NULL DEFAULT FALSE)
  - chat_messages.sender_email (VARCHAR, nullable; backfilled to the owner
    for existing role='user' rows)
  - chat_session_participants table (FK → chat_sessions ON DELETE CASCADE,
    a constraint the DuckDB side cannot express — DuckDB deletes child
    participant rows by hand in ChatRepository.hard_delete_user_sessions).

Revision ID: 0016_cloud_chat_v69
Revises: 0015_cloud_chat_v68
Create Date: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_cloud_chat_v69"
down_revision: Union[str, None] = "0015_cloud_chat_v68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "is_co_session", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "ephemeral", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False
        ),
    )
    op.add_column(
        "chat_messages", sa.Column("sender_email", sa.String(), nullable=True)
    )
    op.execute(
        "UPDATE chat_messages SET sender_email = s.user_email "
        "FROM chat_sessions s "
        "WHERE s.id = chat_messages.session_id "
        "AND chat_messages.role = 'user' "
        "AND chat_messages.sender_email IS NULL"
    )
    op.create_table(
        "chat_session_participants",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            sa.String(),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("session_id", "user_email", name="uq_participant_session_user"),
    )
    op.create_index(
        "idx_chat_session_participants_user",
        "chat_session_participants",
        ["user_email", "session_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_chat_session_participants_user", "chat_session_participants")
    op.drop_table("chat_session_participants")
    op.drop_column("chat_messages", "sender_email")
    op.drop_column("chat_sessions", "ephemeral")
    op.drop_column("chat_sessions", "is_co_session")
