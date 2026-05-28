"""resource_grants — per-type FK columns for 5 of 6 ResourceTypes.

Implements E.3 from the round-2 RBAC review: add one NULLable FK column
per typed ResourceType so orphan grants are caught at the DB layer rather
than deferred to application code.

Five typed columns are added, each referencing the parent table with
ON DELETE CASCADE:

  resource_id_table          -> table_registry(id)
  resource_id_data_package   -> data_packages(id)
  resource_id_memory_domain  -> memory_domains(id)
  resource_id_memory_item    -> knowledge_items(id)
  resource_id_recipe         -> recipes(id)

A CHECK constraint enforces the polymorphic invariant: exactly the
column that corresponds to resource_type must be non-NULL; all others
must be NULL.  The sixth ResourceType (marketplace_plugin) uses a
composite ``<slug>/<plugin_name>`` path in the legacy resource_id column
and therefore matches the "all per-type columns NULL" branch of the CHECK.

The legacy resource_id column is NOT dropped — existing queries and
application-layer code continue to read from it.  For the 5 typed rows
both resource_id AND the per-type column carry the same value; the
per-type column is FK-enforced, resource_id is the backwards-compatible
lookup column.

Existing rows are backfilled: resource_id is copied into the per-type
column inferred from resource_type.  marketplace_plugin rows are skipped
(all per-type columns stay NULL; resource_id remains the source of truth).

Revision ID: 0013_resource_grants_per_type_fk
Revises: 0012_duckdb_v59_parity
Create Date: 2026-05-28

Merge note: if main ships a v60 telemetry migration before this branch
merges, rename this revision to 0014 and update down_revision accordingly
to keep the Alembic chain linear.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013_resource_grants_per_type_fk"
down_revision: Union[str, None] = "0012_duckdb_v59_parity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Add the five per-type nullable FK columns ---
    op.add_column(
        "resource_grants",
        sa.Column("resource_id_table", sa.String(), nullable=True),
    )
    op.add_column(
        "resource_grants",
        sa.Column("resource_id_data_package", sa.String(), nullable=True),
    )
    op.add_column(
        "resource_grants",
        sa.Column("resource_id_memory_domain", sa.String(), nullable=True),
    )
    op.add_column(
        "resource_grants",
        sa.Column("resource_id_memory_item", sa.String(), nullable=True),
    )
    op.add_column(
        "resource_grants",
        sa.Column("resource_id_recipe", sa.String(), nullable=True),
    )

    # --- Foreign key constraints (ON DELETE CASCADE) ---
    op.create_foreign_key(
        "fk_resource_grants_table",
        "resource_grants",
        "table_registry",
        ["resource_id_table"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_resource_grants_data_package",
        "resource_grants",
        "data_packages",
        ["resource_id_data_package"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_resource_grants_memory_domain",
        "resource_grants",
        "memory_domains",
        ["resource_id_memory_domain"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_resource_grants_memory_item",
        "resource_grants",
        "knowledge_items",
        ["resource_id_memory_item"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_resource_grants_recipe",
        "resource_grants",
        "recipes",
        ["resource_id_recipe"],
        ["id"],
        ondelete="CASCADE",
    )

    # --- CHECK constraint: polymorphic invariant ---
    # For the 5 FK-typed ResourceTypes: exactly the matching per-type column
    # must be non-NULL; all other per-type columns must be NULL.
    # For marketplace_plugin and any future / unknown resource_type: all five
    # per-type columns must be NULL (application layer validates the id).
    op.create_check_constraint(
        "ck_resource_grants_per_type_fk",
        "resource_grants",
        """
        (resource_type = 'table'
            AND resource_id_table           IS NOT NULL
            AND resource_id_data_package    IS NULL
            AND resource_id_memory_domain   IS NULL
            AND resource_id_memory_item     IS NULL
            AND resource_id_recipe          IS NULL)
        OR
        (resource_type = 'data_package'
            AND resource_id_table           IS NULL
            AND resource_id_data_package    IS NOT NULL
            AND resource_id_memory_domain   IS NULL
            AND resource_id_memory_item     IS NULL
            AND resource_id_recipe          IS NULL)
        OR
        (resource_type = 'memory_domain'
            AND resource_id_table           IS NULL
            AND resource_id_data_package    IS NULL
            AND resource_id_memory_domain   IS NOT NULL
            AND resource_id_memory_item     IS NULL
            AND resource_id_recipe          IS NULL)
        OR
        (resource_type = 'memory_item'
            AND resource_id_table           IS NULL
            AND resource_id_data_package    IS NULL
            AND resource_id_memory_domain   IS NULL
            AND resource_id_memory_item     IS NOT NULL
            AND resource_id_recipe          IS NULL)
        OR
        (resource_type = 'recipe'
            AND resource_id_table           IS NULL
            AND resource_id_data_package    IS NULL
            AND resource_id_memory_domain   IS NULL
            AND resource_id_memory_item     IS NULL
            AND resource_id_recipe          IS NOT NULL)
        OR
        (resource_type NOT IN ('table', 'data_package', 'memory_domain', 'memory_item', 'recipe')
            AND resource_id_table           IS NULL
            AND resource_id_data_package    IS NULL
            AND resource_id_memory_domain   IS NULL
            AND resource_id_memory_item     IS NULL
            AND resource_id_recipe          IS NULL)
        """,
    )

    # --- Backfill existing rows ---
    # Copy resource_id into the appropriate per-type column based on
    # resource_type. marketplace_plugin rows are left with all per-type
    # columns NULL (they stay application-validated).
    op.execute(
        sa.text(
            """
            UPDATE resource_grants
            SET resource_id_table = resource_id
            WHERE resource_type = 'table'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE resource_grants
            SET resource_id_data_package = resource_id
            WHERE resource_type = 'data_package'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE resource_grants
            SET resource_id_memory_domain = resource_id
            WHERE resource_type = 'memory_domain'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE resource_grants
            SET resource_id_memory_item = resource_id
            WHERE resource_type = 'memory_item'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE resource_grants
            SET resource_id_recipe = resource_id
            WHERE resource_type = 'recipe'
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("ck_resource_grants_per_type_fk", "resource_grants", type_="check")
    op.drop_constraint("fk_resource_grants_recipe", "resource_grants", type_="foreignkey")
    op.drop_constraint("fk_resource_grants_memory_item", "resource_grants", type_="foreignkey")
    op.drop_constraint("fk_resource_grants_memory_domain", "resource_grants", type_="foreignkey")
    op.drop_constraint("fk_resource_grants_data_package", "resource_grants", type_="foreignkey")
    op.drop_constraint("fk_resource_grants_table", "resource_grants", type_="foreignkey")
    op.drop_column("resource_grants", "resource_id_recipe")
    op.drop_column("resource_grants", "resource_id_memory_item")
    op.drop_column("resource_grants", "resource_id_memory_domain")
    op.drop_column("resource_grants", "resource_id_data_package")
    op.drop_column("resource_grants", "resource_id_table")
