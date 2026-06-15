"""source connections + connection secrets (DuckDB v77)

Revision ID: 0024_source_connections_v77
Revises: 0023_store_entity_votes_v76
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0024_source_connections_v77"
down_revision: str = "0023_store_entity_votes_v76"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_connections",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("token_env", sa.String(), nullable=True),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.text("FALSE"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_table(
        "connection_secrets",
        sa.Column("connection_id", sa.String(), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.add_column("table_registry", sa.Column("connection_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("table_registry", "connection_id")
    op.drop_table("connection_secrets")
    op.drop_table("source_connections")
