"""memory_mining_consent (DuckDB v78 parity).

Per-user opt-IN to having their session transcripts mined into shared corporate
memory. Privacy gate (design spec §4.4) — the miner only reads transcripts whose
author positively opted in.

Mirrors DuckDB ``_v77_to_v78``. Additive-only — a brand-new table.

Revision ID: 0028_memory_mining_consent_v81
Revises: 0027_authoring_suggestions_v80
Create Date: 2026-06-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_memory_mining_consent_v81"
down_revision: Union[str, None] = "0027_authoring_suggestions_v80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memory_mining_consent",
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("opted_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_email"),
    )


def downgrade() -> None:
    op.drop_table("memory_mining_consent")
