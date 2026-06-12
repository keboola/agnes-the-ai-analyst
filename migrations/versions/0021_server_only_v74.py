"""table_registry.server_only distribution flag (DuckDB v74 parity).

A non-null boolean (default false) decoupling distribution from
``query_mode``: a ``server_only=true`` row is kept server-side and stays
queryable via ``agnes query --remote``, but ``agnes pull`` does NOT
download its parquet. Only meaningful for query_mode IN
('local', 'materialized'); ignored for 'remote'.

Mirrors DuckDB ``_v73_to_v74``. Additive-only; the server_default keeps
every existing row at false.

Revision ID: 0021_server_only_v74
Revises: 0020_chat_sandbox_refs_v73
Create Date: 2026-06-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_server_only_v74"
down_revision: Union[str, None] = "0020_chat_sandbox_refs_v73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "table_registry",
        sa.Column(
            "server_only",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("table_registry", "server_only")
