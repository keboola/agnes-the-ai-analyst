"""user_journey_state.agent_created — sixth onboarding step

Mirrors DuckDB ``_v115_to_v116``. Adds the ``agent_created`` boolean to
``user_journey_state`` so the "Create your first agent" checklist step is
backed by a real column.

The checklist step had no backing column, so it could not be tracked. The
BACKFILL is the substance of this migration, not the column: a flag landing
FALSE for everyone re-opens a checklist people had already finished, which
reads as the product losing their progress. Anyone who owns an agent, or who
is already flagged ``onboarded``, is marked done — see the DuckDB twin for the
full reasoning.

Revision ID: 0063_journey_agent_created_v116
Revises: 0062_knowledge_domains_backfill
Create Date: 2026-08-13

Note on the revision id: ``alembic_version.version_num`` is ``VARCHAR(32)``,
so keep new ids short (see 0051_store_publisher_v104's note).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0063_journey_agent_created_v116"
down_revision: Union[str, None] = "0062_knowledge_domains_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_journey_state" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("user_journey_state")}
    if "agent_created" in existing_cols:
        return
    op.add_column(
        "user_journey_state",
        sa.Column(
            "agent_created",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Same backfill as the DuckDB ladder, or the two engines disagree about
    # whose checklist re-opens.
    op.execute("UPDATE user_journey_state SET agent_created = TRUE WHERE onboarded = TRUE")
    if "agents" in set(insp.get_table_names()):
        op.execute(
            "UPDATE user_journey_state SET agent_created = TRUE "
            "WHERE user_id IN (SELECT DISTINCT owner_user_id FROM agents)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_journey_state" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("user_journey_state")}
    if "agent_created" in existing_cols:
        op.drop_column("user_journey_state", "agent_created")
