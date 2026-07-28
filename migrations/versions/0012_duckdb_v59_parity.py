"""DuckDB v59 schema parity — missing columns and tables.

Brings the PG schema to parity with the live DuckDB v59 schema on
agnes-dev so the DB-state-machine migrator can copy every row.  The
gap was found during the agnes-dev live deploy verification (PR #455):
``column "requirement" of relation "resource_grants" does not exist``
plus four DuckDB tables with no PG model (claude_md_template,
welcome_template, setup_banner, session_extraction_state).

Adds:
  - resource_grants.requirement           VARCHAR  (NULL OK)
  - knowledge_items.is_required           BOOLEAN  (NULL OK)
  - store_entities.title/tagline/synthetic_name  VARCHAR (NULL OK)
  - table_registry.{bq_fqn, partition_col, grain, platforms, history,
                     gotchas, things_to_know}             VARCHAR/TEXT
  - table_registry.{sample_questions, pairs_well_with}    JSONB
  - 4 new tables for legacy single-row templates + session ingestion checkpoint

The legacy single-row tables (claude_md_template, welcome_template,
setup_banner) are mirrored 1:1 from DuckDB rather than consolidated
into ``instance_templates`` — that consolidation would require dual
writes from every code path during the cutover and is deferred to a
follow-up PR.

Revision ID: 0012_duckdb_v59_parity
Revises: 0011_data_packages
Create Date: 2026-05-28

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0012_duckdb_v59_parity"
down_revision: Union[str, None] = "0011_data_packages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Column additions (drift fixes) ---
    op.add_column(
        "resource_grants",
        sa.Column("requirement", sa.String(), nullable=True),
    )
    op.add_column(
        "knowledge_items",
        sa.Column("is_required", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "store_entities",
        sa.Column("title", sa.String(), nullable=True),
    )
    op.add_column(
        "store_entities",
        sa.Column("tagline", sa.String(), nullable=True),
    )
    op.add_column(
        "store_entities",
        sa.Column("synthetic_name", sa.String(), nullable=True),
    )

    # table_registry — 9 new columns
    op.add_column("table_registry", sa.Column("bq_fqn", sa.String(), nullable=True))
    op.add_column("table_registry", sa.Column("partition_col", sa.String(), nullable=True))
    op.add_column("table_registry", sa.Column("grain", sa.String(), nullable=True))
    op.add_column("table_registry", sa.Column("platforms", sa.String(), nullable=True))
    op.add_column("table_registry", sa.Column("history", sa.Text(), nullable=True))
    op.add_column("table_registry", sa.Column("gotchas", sa.Text(), nullable=True))
    op.add_column("table_registry", sa.Column("things_to_know", sa.Text(), nullable=True))
    op.add_column("table_registry", sa.Column("sample_questions", JSONB(), nullable=True))
    op.add_column("table_registry", sa.Column("pairs_well_with", JSONB(), nullable=True))

    # --- New tables ---
    op.create_table(
        "claude_md_template",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "welcome_template",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "setup_banner",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "session_extraction_state",
        sa.Column("session_file", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("items_extracted", sa.Integer(), nullable=True),
        sa.Column("file_hash", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("session_file"),
    )
    op.create_index(
        "ix_session_extraction_state_username",
        "session_extraction_state",
        ["username"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_extraction_state_username", table_name="session_extraction_state")
    op.drop_table("session_extraction_state")
    op.drop_table("setup_banner")
    op.drop_table("welcome_template")
    op.drop_table("claude_md_template")

    for col in (
        "pairs_well_with", "sample_questions", "things_to_know", "gotchas",
        "history", "platforms", "grain", "partition_col", "bq_fqn",
    ):
        op.drop_column("table_registry", col)
    op.drop_column("store_entities", "synthetic_name")
    op.drop_column("store_entities", "tagline")
    op.drop_column("store_entities", "title")
    op.drop_column("knowledge_items", "is_required")
    op.drop_column("resource_grants", "requirement")
