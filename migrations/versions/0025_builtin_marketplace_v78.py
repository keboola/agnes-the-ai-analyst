"""builtin_marketplace (DuckDB v77 parity).

Adds ``marketplace_registry.is_builtin`` (BOOLEAN NOT NULL DEFAULT FALSE) so the
system-seeded built-in marketplace row is distinguishable from admin-registered
rows and can be skipped by the nightly git-sync path.

Adds ``marketplace_plugins.admin_disabled`` (BOOLEAN NOT NULL DEFAULT FALSE) for
per-plugin admin disable of built-in plugins. Disabled plugins are filtered from
the served feed for all callers regardless of their RBAC grants.

Mirrors DuckDB ``_v76_to_v77``. Additive-only — two new columns on existing tables.

Revision ID: 0024_builtin_marketplace_v77
Revises: 0023_store_entity_votes_v76
Create Date: 2026-06-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_builtin_marketplace_v78"
down_revision: Union[str, None] = "0024_must_change_password_v77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "marketplace_registry",
        sa.Column(
            "is_builtin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "marketplace_plugins",
        sa.Column(
            "admin_disabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("marketplace_plugins", "admin_disabled")
    op.drop_column("marketplace_registry", "is_builtin")
