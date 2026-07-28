"""Slack identity binding column (DuckDB v71 parity).

Additive-only, reaching the same endpoint as DuckDB ``_v70_to_v71``:
  - users.slack_user_id (VARCHAR, nullable)

Maps a Slack ``user_id`` to an Agnes account so the Slack bot can resolve
who is talking. Previously this column was lazily ``ALTER``-ed only into the
DuckDB system file by ``services/slack_bot/binding.py``, so it never existed
on a Postgres instance and Slack identity binding silently failed there.

Revision ID: 0018_slack_user_id_v71
Revises: 0017_cloud_chat_v70
Create Date: 2026-06-04
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_slack_user_id_v71"
down_revision: Union[str, None] = "0017_cloud_chat_v70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("slack_user_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "slack_user_id")
