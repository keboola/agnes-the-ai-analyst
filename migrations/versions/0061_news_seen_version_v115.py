"""user_journey_state.news_seen_version — /news unread-dot indicator (#1053)

Mirrors DuckDB ``_v114_to_v115``. Adds ``news_seen_version`` to
``user_journey_state``: the highest ``news_template.version`` the caller has
acknowledged, PUT alongside every other journey field through the existing
self-scoped ``PUT /api/chat/journey`` endpoint.

No backfill needed: the server default (0) applies to both existing and new
rows, and 0 is lower than any real published version — the first published
news update on an instance lights the dot for everyone until each user
visits ``/news``.

Revision ID: 0061_news_seen_version_v115
Revises: 0060_pkg_publisher_v113
Create Date: 2026-08-10

Note on the revision id: ``alembic_version.version_num`` is ``VARCHAR(32)``,
so keep new ids short (see 0051_store_publisher_v104's note).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0061_news_seen_version_v115"
down_revision: Union[str, None] = "0060_pkg_publisher_v113"
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
            sa.Column("news_seen_version", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_journey_state" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("user_journey_state")}
    if "news_seen_version" in existing:
        op.drop_column("user_journey_state", "news_seen_version")
