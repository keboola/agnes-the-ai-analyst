"""sync_state.parts — per-partition manifest for partitioned tables

Mirrors DuckDB ``_v99_to_v100``. Adds ``parts`` (JSONB, nullable) to
``sync_state``: a list of ``{path, hash, size_bytes}`` describing each
parquet part of a partitioned table (Jira hive, Keboola partitioned).
NULL means a single-file table, so the manifest / ``agnes pull`` keep
treating it as single-file (backward compatible).

Revision ID: 0047_sync_state_parts_v100
Revises: 0046_data_apps_drafts_v99
Create Date: 2026-07-27

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_sync_state_parts_v100"
down_revision: Union[str, None] = "0046_data_apps_drafts_v99"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sync_state", sa.Column("parts", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("sync_state", "parts")
