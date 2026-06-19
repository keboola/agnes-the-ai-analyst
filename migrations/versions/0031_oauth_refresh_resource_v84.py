"""Add resource column to oauth_refresh_tokens (DuckDB v84)

Refreshed access tokens must preserve the original token's `resource`
binding (RFC 8707). The auth-code exchange persists `resource` on the
access token; the refresh path needs the same value carried on the
refresh-token row so token rotation doesn't drop it.

Revision ID: 0031_oauth_refresh_resource_v84
Revises: 0030_oauth_clients_v83
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0031_oauth_refresh_resource_v84"
down_revision: str = "0030_oauth_clients_v83"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oauth_refresh_tokens",
        sa.Column("resource", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("oauth_refresh_tokens", "resource")
