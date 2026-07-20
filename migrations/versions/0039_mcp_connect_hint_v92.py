"""mcp_sources.connect_hint (schema v92)

Revision ID: 0039_mcp_connect_hint_v92
Revises: 0038_store_lint_v91
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0039_mcp_connect_hint_v92"
down_revision: str = "0038_store_lint_v91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_sources", sa.Column("connect_hint", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_sources", "connect_hint")
