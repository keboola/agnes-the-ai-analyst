"""system_secrets table (DuckDB v72 parity).

Server-wide vault for system-level secrets keyed by name (Slack bot tokens).
Mirrors DuckDB ``_v71_to_v72``. Additive-only.

Revision ID: 0019_system_secrets_v72
Revises: 0018_slack_user_id_v71
Create Date: 2026-06-04
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_system_secrets_v72"
down_revision: Union[str, None] = "0018_slack_user_id_v71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_secrets",
        sa.Column("name", sa.String(), primary_key=True, nullable=False),
        sa.Column("secret_value_enc", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("system_secrets")
