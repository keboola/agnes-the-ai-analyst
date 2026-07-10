"""Add ref (tag/commit pin) column to marketplace_registry (DuckDB v87, issue #781)

Pins a registered marketplace to a fixed tag name or full 40-char commit
SHA so nightly/manual syncs stop tracking `branch` (or remote HEAD) once
set. Mutually exclusive with `branch`, enforced at the admin API layer.

Revision ID: 0034_marketplace_ref_pin_v87
Revises: 0033_everyone_backfill_v86
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0034_marketplace_ref_pin_v87"
down_revision: str = "0033_everyone_backfill_v86"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "marketplace_registry",
        sa.Column("ref", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("marketplace_registry", "ref")
