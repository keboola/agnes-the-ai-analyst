"""user_journey_state.news_seen_version — rail "What's new" unread marker

Mirrors DuckDB ``_v112_to_v113``. Adds a per-user integer tracking the last
``news_template.version`` the user has seen: the rail's unread dot lights up
when the latest published version exceeds this. Defaults to 0, which is
<= any real published version, so every pre-v113 row starts unread until the
user opens ``/news`` once.

Revision ID: 0060_news_seen_version_v113
Revises: 0059_chat_pinned_v112
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0060_news_seen_version_v113"
down_revision: Union[str, None] = "0059_chat_pinned_v112"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_journey_state" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("user_journey_state")}
    if "news_seen_version" not in existing:
        op.add_column(
            "user_journey_state",
            sa.Column("news_seen_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_journey_state" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("user_journey_state")}
    if "news_seen_version" in existing:
        op.drop_column("user_journey_state", "news_seen_version")
