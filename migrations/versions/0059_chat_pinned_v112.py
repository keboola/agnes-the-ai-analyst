"""chat_sessions.pinned_at — user-pinned conversations

Mirrors DuckDB ``_v111_to_v112``. Adds a nullable timestamp to
``chat_sessions``: NULL = not pinned (every pre-v112 row), a timestamp = pinned
at that moment. A timestamp rather than a boolean so the history panel can order
pins most-recently-pinned-first.

Deliberately un-indexed, matching the DuckDB sibling: the column is UPDATEd on
every pin/unpin, and on DuckDB 1.5.3 indexing a ``chat_sessions`` column that is
UPDATEd after ``chat_messages`` rows exist trips the FK+index false-violation
bug. Pin state is only ever read as part of a single user's session list, which
is already covered by ``idx_chat_sessions_user``.

Revision ID: 0059_chat_pinned_v112
Revises: 0057_agents_superset_v110
Create Date: 2026-07-31

Note on the revision id: ``alembic_version.version_num`` is ``VARCHAR(32)``,
so keep new ids short (see 0051_store_publisher_v104's note).

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0059_chat_pinned_v112"
down_revision: Union[str, None] = "0057_agents_superset_v110"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chat_sessions" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("chat_sessions")}
    if "pinned_at" not in existing:
        op.add_column(
            "chat_sessions",
            sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "chat_sessions" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("chat_sessions")}
    if "pinned_at" in existing:
        op.drop_column("chat_sessions", "pinned_at")
