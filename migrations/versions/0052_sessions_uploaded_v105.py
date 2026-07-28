"""usage_session_summary.uploaded_at — arrival anchor for the sessions browser

Mirrors DuckDB ``_v104_to_v105``. Adds ``uploaded_at`` (TIMESTAMP, nullable,
NO secondary index — the v95 ART-index incident applies to update-heavy
columns on this table in DuckDB; keeping both engines index-symmetric).
Backfills from the newest ``session.upload`` audit row whose params
filename matches the summary's ``session_file`` basename (join on the FILE
name, never session_id — resumed/forked sessions carry a different
content-derived id), falling back to ``started_at``.

Revision ID: 0052_sessions_uploaded_v105
Revises: 0051_audit_id_backfill_v104
Create Date: 2026-07-28

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0052_sessions_uploaded_v105"
down_revision: Union[str, None] = "0051_audit_id_backfill_v104"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "usage_session_summary",
        sa.Column("uploaded_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE usage_session_summary SET uploaded_at = (
                SELECT max(a.timestamp) FROM audit_log a
                WHERE a.action = 'session.upload'
                  AND CAST(a.params AS TEXT) LIKE
                      '%' || substr(
                          usage_session_summary.session_file,
                          position('/' in usage_session_summary.session_file) + 1
                      ) || '%'
            ) WHERE uploaded_at IS NULL
            """
        )
    )
    op.execute(
        sa.text("UPDATE usage_session_summary SET uploaded_at = COALESCE(uploaded_at, started_at, CURRENT_TIMESTAMP)")
    )


def downgrade() -> None:
    op.drop_column("usage_session_summary", "uploaded_at")
