"""OAuth 2.1 client registrations and token tables (DuckDB v80)

Revision ID: 0027_oauth_clients_v80
Revises: 0026_source_connections_v79
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0027_oauth_clients_v80"
down_revision: str = "0026_source_connections_v79"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(), primary_key=True),
        sa.Column("client_secret", sa.String(), nullable=True),
        sa.Column("redirect_uris", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("client_name", sa.String(), nullable=True),
        sa.Column("client_metadata", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_table(
        "oauth_auth_codes",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("code_challenge", sa.String(), nullable=False),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column(
            "redirect_uri_provided_explicitly",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("resource", sa.String(), nullable=True),
    )
    op.create_table(
        "oauth_access_tokens",
        sa.Column("token", sa.String(), primary_key=True),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("resource", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_table(
        "oauth_refresh_tokens",
        sa.Column("token", sa.String(), primary_key=True),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("oauth_refresh_tokens")
    op.drop_table("oauth_access_tokens")
    op.drop_table("oauth_auth_codes")
    op.drop_table("oauth_clients")
