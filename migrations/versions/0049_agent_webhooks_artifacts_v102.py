"""agent webhooks + artifacts (DuckDB v102).

Revision ID: 0049_webhooks_artifacts_v102 (drops the "agent_" segment —
alembic_version.version_num is VARCHAR(32))
Revises: 0048_agents_v101

Mirrors DuckDB's ``_v101_to_v102`` / ``_SYSTEM_SCHEMA`` additions (agent-api
V1b). Creates the outbound webhook registration table (``agent_webhooks``,
HMAC-signed POSTs on a comma-joined ``events`` list, secret shown once at
create like a PAT) and the run-artifact metadata table (``agent_artifacts``,
blob lives in the object store under ``object_key`` — this row is metadata
only).

No ``op.create_index`` calls anywhere in this revision — see the
``_v94_to_v95`` docstring for the DuckDB ART-index incident this repo avoids
repeating.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0049_webhooks_artifacts_v102"
down_revision: Union[str, None] = "0048_agents_v101"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_webhooks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), nullable=False),
        sa.Column("events", sa.String(), server_default=sa.text("'job.completed,job.failed'"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
    )

    op.create_table(
        "agent_artifacts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("md5", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("agent_artifacts")
    op.drop_table("agent_webhooks")
