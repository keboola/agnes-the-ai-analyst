"""chat_broker_tickets table — chat sandbox secret broker tickets (DuckDB v90 parity)

Opaque, short-lived tickets (`secrets.token_urlsafe(32)`) minted by
``ticket_repo().mint(session_id, scope, ttl_seconds)`` and resolved by the
broker routes so a sandboxed chat agent never holds the real
``ANTHROPIC_API_KEY`` / ``AGNES_TOKEN``. Indexed on ``session_id`` so
``revoke_session`` can invalidate every outstanding ticket for a session in
one statement.

Mirrors DuckDB ``_v89_to_v90``. Additive only; downgrade drops the table.

Revision ID: 0037_chat_broker_tickets_v90
Revises: 0036_knowledge_digests_v89
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037_chat_broker_tickets_v90"
down_revision: Union[str, None] = "0036_knowledge_digests_v89"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_broker_tickets",
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_index(
        "ix_chat_broker_tickets_session_id",
        "chat_broker_tickets",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_broker_tickets_session_id", "chat_broker_tickets")
    op.drop_table("chat_broker_tickets")
