"""drop usage_session_summary secondary indexes (index-corruption hotfix)

Mirrors DuckDB ``_v94_to_v95``. Drops the 3 non-unique secondary indexes on
``usage_session_summary`` — ``idx_usage_session_user`` (username),
``idx_usage_session_started`` (started_at), ``idx_usage_session_user_id``
(user_id). ``upsert_summary``'s ON CONFLICT DO UPDATE refreshes all
three columns on every re-process tick; on DuckDB, updating an ART-indexed
column runs as delete-old-entry + insert-new-entry, and a corrupt entry
turned that into a FATAL, connection-invalidating error (INCIDENT
2026-07-20). Postgres never hit that failure mode, but the write path is
shared code (dual-backend parity) so the schema stays identical across
engines. The ``session_file`` primary key is untouched.

Revision ID: 0042_usage_summary_idx_fix_v95
Revises: 0041_jobs_v94
Create Date: 2026-07-20

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0042_usage_summary_idx_fix_v95"
down_revision: Union[str, None] = "0041_jobs_v94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("idx_usage_session_user_id", table_name="usage_session_summary")
    op.drop_index("idx_usage_session_started", table_name="usage_session_summary")
    op.drop_index("idx_usage_session_user", table_name="usage_session_summary")


def downgrade() -> None:
    op.create_index("idx_usage_session_user", "usage_session_summary", ["username"])
    op.create_index("idx_usage_session_started", "usage_session_summary", ["started_at"])
    op.create_index("idx_usage_session_user_id", "usage_session_summary", ["user_id"])
