"""agent memories (DuckDB v102).

Revision ID: 0049_agent_memories_v102
Revises: 0048_webhooks_artifacts_v101

Mirrors DuckDB's ``_v101_to_v102`` / ``_SYSTEM_SCHEMA`` addition (agent-api
V1c). Creates the per-agent private memory notebook (``agent_memories``,
``status`` lifecycle ``pending -> active -> archived``; ``owner_user_id``
denormalized for cheap owner-scoped listing).

No ``op.create_index`` calls anywhere in this revision — see the
``_v94_to_v95`` docstring for the DuckDB ART-index incident this repo avoids
repeating.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0049_agent_memories_v102"
down_revision: Union[str, None] = "0048_webhooks_artifacts_v101"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_memories",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_session_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("agent_memories")
