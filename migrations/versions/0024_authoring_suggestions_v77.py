"""authoring_suggestions (DuckDB v77 parity).

Generic non-admin suggestion queue for the authoring studio
(data-package / mcp / marketplace / corporate-memory). A non-admin submits a
proposed create payload; an admin approves (replays it through the real
endpoint) or rejects. Generalizes ``memory_domain_suggestions`` across domains.

Mirrors DuckDB ``_v76_to_v77``. Additive-only — a brand-new table.

Revision ID: 0024_authoring_suggestions_v77
Revises: 0023_store_entity_votes_v76
Create Date: 2026-06-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_authoring_suggestions_v77"
down_revision: Union[str, None] = "0023_store_entity_votes_v76"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "authoring_suggestions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending", nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("created_resource_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_authoring_suggestions_status", "authoring_suggestions", ["status"])


def downgrade() -> None:
    op.drop_index("idx_authoring_suggestions_status", table_name="authoring_suggestions")
    op.drop_table("authoring_suggestions")
