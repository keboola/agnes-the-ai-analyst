"""metric_definitions.grain becomes nullable (DuckDB parity catch-up)

DuckDB has declared the column ``grain VARCHAR DEFAULT 'monthly'`` — nullable
— since it was introduced, while Postgres got it as ``nullable=False`` in
migration 0005. The two ladders therefore reached different endpoints, and
nobody noticed because every caller passed a grain: the repositories'
``create(grain="monthly")`` Python default filled the hole before the column
ever saw a NULL.

Dropping that Python default (so a metric nobody declared a grain for reports
none, rather than an invented "monthly") is what surfaced the divergence: the
same call now writes NULL on DuckDB and raises NotNullViolation on Postgres.
This is a PG-only catch-up to the shape DuckDB already has — like
``0012_duckdb_v59_parity`` — so there is no matching ``_vN_to_v(N+1)`` step
and ``SCHEMA_VERSION`` does not move.

The ``'monthly'`` server default stays on both backends: it is inert for the
repositories (their INSERTs name the column explicitly) and identical either
side of the parity line.

Revision ID: 0065_metric_grain_nullable
Revises: 0064_sem_models_v117
Create Date: 2026-08-16
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0065_metric_grain_nullable"
down_revision: Union[str, None] = "0064_sem_models_v117"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "metric_definitions",
        "grain",
        existing_type=sa.String(),
        existing_server_default=sa.text("'monthly'::character varying"),
        nullable=True,
    )


def downgrade() -> None:
    # Rows written after the upgrade may hold NULL; the server default cannot
    # backfill them (it only applies to INSERTs that omit the column), so
    # restoring NOT NULL has to fill them first or the ALTER fails.
    op.execute("UPDATE metric_definitions SET grain = 'monthly' WHERE grain IS NULL")
    op.alter_column(
        "metric_definitions",
        "grain",
        existing_type=sa.String(),
        existing_server_default=sa.text("'monthly'::character varying"),
        nullable=False,
    )
