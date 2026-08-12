"""table_registry access-policy columns (DuckDB v116 parity)

Mirrors DuckDB ``_v115_to_v116``. Five additive, nullable columns backing
the table access policies feature
(``docs/superpowers/specs/2026-08-11-table-access-policies-design.md`` §4):
one SQL policy per table, substituted for that table on every server-side
read to filter rows and mask columns by the caller's identity.
``access_policy_sql IS NULL`` means "no policy" — every enforcement path
short-circuits to today's unfiltered behavior, so this migration alone
changes no runtime behavior.

- ``access_policy_sql``        — the policy SELECT, DuckDB dialect.
- ``access_policy_note``       — admin-facing "why" (mandatory at the API
  layer when a policy is set; not enforced by this migration).
- ``access_policy_updated_at`` / ``access_policy_updated_by`` — last-edit
  convenience columns; ``audit_log`` remains authoritative.
- ``policy_mapping``           — marks this table as referenceable from
  another table's policy body (mapping tables). Defaults false so no
  existing table is retroactively eligible.

Revision ID: 0062_access_policy_columns_v116
Revises: 0061_agent_status_backfill_v115
Create Date: 2026-08-12

Note on the revision id: ``alembic_version.version_num`` is ``VARCHAR(32)``,
so keep new ids short (see 0051_store_publisher_v104's note).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0062_access_policy_columns_v116"
down_revision: Union[str, None] = "0061_agent_status_backfill_v115"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("table_registry", sa.Column("access_policy_sql", sa.String(), nullable=True))
    op.add_column("table_registry", sa.Column("access_policy_note", sa.String(), nullable=True))
    op.add_column(
        "table_registry",
        sa.Column("access_policy_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("table_registry", sa.Column("access_policy_updated_by", sa.String(), nullable=True))
    op.add_column(
        "table_registry",
        sa.Column("policy_mapping", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("table_registry", "policy_mapping")
    op.drop_column("table_registry", "access_policy_updated_by")
    op.drop_column("table_registry", "access_policy_updated_at")
    op.drop_column("table_registry", "access_policy_note")
    op.drop_column("table_registry", "access_policy_sql")
