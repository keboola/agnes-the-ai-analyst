"""users.must_change_password (DuckDB v77 parity).

Forces a password change on first sign-in for accounts whose password was set
by someone else — the seed admin created from SEED_ADMIN_PASSWORD (emailed in
plaintext) and admin-set passwords. Cleared when the user sets their own
password via the reset/setup confirm flow.

Mirrors DuckDB ``_v76_to_v77``. Additive-only — a NOT NULL column with a
server-side DEFAULT FALSE so existing rows backfill cleanly.

Revision ID: 0024_must_change_password_v77
Revises: 0023_store_entity_votes_v76
Create Date: 2026-06-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_must_change_password_v77"
down_revision: Union[str, None] = "0023_store_entity_votes_v76"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("FALSE"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
