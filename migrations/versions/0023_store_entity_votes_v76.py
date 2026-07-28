"""store_entity_votes (DuckDB v76 parity).

Per-user thumbs up/down ratings on store / marketplace entities. Mirrors the
``knowledge_votes`` shape: one row per (entity, user); the repo upserts on
conflict so a re-vote flips the value, and a clear deletes the row.

Mirrors DuckDB ``_v75_to_v76``. Additive-only — a brand-new table.

Revision ID: 0023_store_entity_votes_v76
Revises: 0022_prompt_source_mode_v75
Create Date: 2026-06-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_store_entity_votes_v76"
down_revision: Union[str, None] = "0022_prompt_source_mode_v75"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "store_entity_votes",
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("vote", sa.Integer(), nullable=True),
        sa.Column(
            "voted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("entity_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("store_entity_votes")
