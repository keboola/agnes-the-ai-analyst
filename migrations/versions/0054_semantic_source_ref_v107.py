"""source_ref on metric_definitions + glossary_terms

Mirrors DuckDB ``_v106_to_v107``. Nullable per-connection provenance for the
multi-project Keboola semantic-layer sync (2026-07-28 spec) — later tasks
read/write it via repo kwargs.

Revision ID: 0054_semantic_source_ref_v107
Revises: 0053_pat_surface_v106
Create Date: 2026-07-28

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0054_semantic_source_ref_v107"
down_revision: Union[str, None] = "0053_pat_surface_v106"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("metric_definitions", sa.Column("source_ref", sa.String(), nullable=True))
    op.add_column("glossary_terms", sa.Column("source_ref", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("glossary_terms", "source_ref")
    op.drop_column("metric_definitions", "source_ref")
