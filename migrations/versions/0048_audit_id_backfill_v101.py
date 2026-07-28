"""audit_log identity backfill — email user_id values → users.id

Mirrors DuckDB ``_v100_to_v101``. Several writers historically stored an
email in ``audit_log.user_id`` (chat, corporate-memory governance,
authoring suggestions, Slack binding), splitting one person into two
Activity Center facet entries. Rewrite each email to the matching
``users.id`` when the email resolves to exactly one account
(case-insensitive); unresolvable or ambiguous emails stay as-is — a
searchable email beats a dropped row.

Data-only migration: no schema change, no downgrade action (the rewrite
is not reversible and losing it is harmless).

Revision ID: 0048_audit_id_backfill_v101
Revises: 0047_sync_state_parts_v100
Create Date: 2026-07-28

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0048_audit_id_backfill_v101"
down_revision: Union[str, None] = "0047_sync_state_parts_v100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE audit_log SET user_id = (
                SELECT min(u.id) FROM users u
                WHERE lower(u.email) = lower(audit_log.user_id)
            )
            WHERE user_id LIKE '%@%'
              AND (
                SELECT COUNT(*) FROM users u
                WHERE lower(u.email) = lower(audit_log.user_id)
              ) = 1
            """
        )
    )


def downgrade() -> None:
    pass
