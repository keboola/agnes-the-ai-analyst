"""glossary_terms table — Keboola semantic-glossary import destination

Mirrors DuckDB ``_v92_to_v93``. Additive only; downgrade drops the table.

Revision ID: 0040_glossary_terms_v93
Revises: 0039_mcp_connect_hint_v92
Create Date: 2026-07-17

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0040_glossary_terms_v93"
down_revision: Union[str, None] = "0039_mcp_connect_hint_v92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "glossary_terms",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("term", sa.String(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("see_also", ARRAY(sa.String()), nullable=True),
        sa.Column("model_uuid", sa.String(), nullable=True),
        sa.Column("source", sa.String(), server_default=sa.text("'manual'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("glossary_terms")
