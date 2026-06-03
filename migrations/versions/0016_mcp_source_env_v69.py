"""v69: per-source env vars on mcp_sources.

Adds ``mcp_sources.env`` (JSONB, nullable) — non-secret env vars for the
stdio subprocess. Mirrors the ``args`` column's JSONB type. Parity with the
DuckDB ``_v68_to_v69`` step.

Revision ID: 0016_mcp_source_env_v69
Revises: 0015_cloud_chat_v68
Create Date: 2026-06-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0016_mcp_source_env_v69"
down_revision: Union[str, None] = "0015_cloud_chat_v68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mcp_sources", sa.Column("env", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_sources", "env")
