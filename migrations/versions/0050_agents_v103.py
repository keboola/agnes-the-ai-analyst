"""agents registry (server-side agent definitions)

Mirrors DuckDB ``_v102_to_v103``. The Agent builder (/agents) previously kept
agent definitions in ``localStorage`` only, so an agent could not be listed on
another device, surfaced in the Library, or shared with a group. This table is
the registry those need.

The JSON-encoded columns (``knowledge``, ``plugins``, ``surfaces``) mirror the
builder's in-browser shape 1:1 — opaque id lists the builder owns, never joined
against in SQL.

Idempotent + guarded so it's a no-op where the table already exists.

Revision ID: 0050_agents_v103
Revises: 0049_file_corpora_origin_v102
Create Date: 2026-07-28

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0050_agents_v103"
down_revision: Union[str, None] = "0049_file_corpora_origin_v102"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "agents" in insp.get_table_names():
        return
    op.create_table(
        "agents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), server_default=""),
        sa.Column("instructions", sa.Text(), server_default=""),
        sa.Column("tone", sa.String(), server_default="concise"),
        sa.Column("greeting", sa.Text(), server_default=""),
        sa.Column("knowledge", sa.Text(), server_default="[]"),
        sa.Column("plugins", sa.Text(), server_default="[]"),
        sa.Column("surfaces", sa.Text(), server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The Library and the builder both list "my agents" — created_by is the
    # only access-scoping predicate either one filters on.
    op.create_index("idx_agents_created_by", "agents", ["created_by"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "agents" not in insp.get_table_names():
        return
    existing = {ix["name"] for ix in insp.get_indexes("agents")}
    if "idx_agents_created_by" in existing:
        op.drop_index("idx_agents_created_by", table_name="agents")
    op.drop_table("agents")
