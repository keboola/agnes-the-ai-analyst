"""agent_schedules — scheduled runs for agent profiles (DuckDB v120).

Mirrors DuckDB's ``_v119_to_v120`` / ``_SYSTEM_SCHEMA`` addition. Design doc:
docs/superpowers/specs/2026-08-17-agent-schedules-design.md.

No ``op.create_index`` calls anywhere in this revision — see the
``_v94_to_v95`` docstring for the DuckDB ART-index incident this repo avoids
repeating.

Revision ID: 0068_agent_schedules_v120
Revises: 0067_tool_projection_map_v119
Create Date: 2026-08-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0068_agent_schedules_v120"
down_revision: Union[str, None] = "0067_tool_projection_map_v119"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_schedules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("schedule", sa.String(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(), nullable=True),
        sa.Column("last_job_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.UniqueConstraint("agent_id", "name", name="uq_agent_schedules_agent_name"),
    )


def downgrade() -> None:
    op.drop_table("agent_schedules")
